from flask import Flask, Blueprint, redirect, url_for, session, request, jsonify, current_app
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_cors import CORS
from authlib.integrations.flask_client import OAuth
from ppadb.client import Client as AdbClient
from dotenv import load_dotenv
import logging
import os
import time
from typing import Dict, Optional
from urllib.parse import quote as url_quote

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Configuration class
class Config:
    """Application configuration."""
    
    # Flask settings
    SECRET_KEY = os.getenv('SECRET_KEY', 'my-secret-key-for-development')
    DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'
    
    # Google OAuth settings
    GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
    GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
    
    # ADB settings
    ANDROID_TV_IP = os.getenv('ANDROID_TV_IP', '192.168.1.100')
    ANDROID_TV_PORT = int(os.getenv('ANDROID_TV_PORT', '5555'))
    
    # Security settings
    ALLOWED_EMAILS = os.getenv('ALLOWED_EMAILS', '').split(',')

# Android key codes
KEYCODE_POWER = 26
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_MUTE = 164
KEYCODE_DPAD_UP = 19
KEYCODE_DPAD_DOWN = 20
KEYCODE_DPAD_LEFT = 21
KEYCODE_DPAD_RIGHT = 22
KEYCODE_DPAD_CENTER = 23  # OK button
KEYCODE_BACK = 4
KEYCODE_HOME = 3

class TVController:
    """Android TV controller using ADB."""
    
    def __init__(self, host: str, port: int = 5555):
        self.host = host
        self.port = port
        self.client = AdbClient(host="127.0.0.1", port=5037)
        self.device = None
    
    def connect(self) -> bool:
        try:
            device = self.client.connect(self.host, self.port)
            if device and device.get_serial_no():
                self.device = device
                logger.info(f"Connected to {self.host}:{self.port}")
                return True
            logger.error(f"Failed to connect to {self.host}:{self.port}")
            return False
        except Exception as e:
            logger.error(f"Error connecting to Android TV: {str(e)}")
            return False
    
    def ensure_connected(self) -> bool:
        if self.device:
            try:
                self.device.shell('echo test')
                return True
            except:
                logger.info("Connection lost, attempting to reconnect...")
                return self.connect()
        else:
            return self.connect()
    
    def send_keyevent(self, keycode: int) -> bool:
        if not self.ensure_connected():
            return False
        try:
            self.device.shell(f'input keyevent {keycode}')
            return True
        except Exception as e:
            logger.error(f"Error sending keyevent {keycode}: {str(e)}")
            return False
    
    def power(self) -> bool:
        return self.send_keyevent(KEYCODE_POWER)
    
    def volume_up(self) -> bool:
        return self.send_keyevent(KEYCODE_VOLUME_UP)
    
    def volume_down(self) -> bool:
        return self.send_keyevent(KEYCODE_VOLUME_DOWN)
    
    def mute(self) -> bool:
        return self.send_keyevent(KEYCODE_MUTE)
    
    def navigate(self, direction: str) -> bool:
        keycode_map = {
            'up': KEYCODE_DPAD_UP,
            'down': KEYCODE_DPAD_DOWN,
            'left': KEYCODE_DPAD_LEFT,
            'right': KEYCODE_DPAD_RIGHT,
            'ok': KEYCODE_DPAD_CENTER,
            'back': KEYCODE_BACK,
            'home': KEYCODE_HOME,
        }
        
        if direction not in keycode_map:
            logger.error(f"Invalid navigation direction: {direction}")
            return False
        
        return self.send_keyevent(keycode_map[direction])
    
    def launch_app(self, package_name: str) -> bool:
        if not self.ensure_connected():
            return False
        try:
            self.device.shell(f'monkey -p {package_name} -c android.intent.category.LAUNCHER 1')
            return True
        except Exception as e:
            logger.error(f"Error launching app {package_name}: {str(e)}")
            return False

# Initialize extensions
login_manager = LoginManager()
oauth = OAuth()

# Create blueprints
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
api_bp = Blueprint('api', __name__, url_prefix='/api')

# User model for Flask-Login
class User(UserMixin):
    def __init__(self, id, email, name, picture):
        self.id = id
        self.email = email
        self.name = name
        self.picture = picture

# User loader for Flask-Login
@login_manager.user_loader
def load_user(user_id):
    if user_id and 'user_info' in session:
        user_info = session['user_info']
        return User(
            id=user_info['sub'],
            email=user_info['email'],
            name=user_info['name'],
            picture=user_info.get('picture', '')
        )
    return None

# TV controller cache
tv_controller_cache = {}

def get_tv_controller():
    """Get or create a TV controller for the current user."""
    user_id = current_user.id
    
    if user_id not in tv_controller_cache:
        host = current_app.config['ANDROID_TV_IP']
        port = current_app.config['ANDROID_TV_PORT']
        
        controller = TVController(host, port)
        if controller.connect():
            tv_controller_cache[user_id] = controller
        else:
            logger.error(f"Failed to create TV controller for user {user_id}")
            return None
    
    return tv_controller_cache[user_id]

# Setup Google OAuth
def setup_oauth(app):
    oauth.init_app(app)
    oauth.register(
        name='google',
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        server_metadata_url=app.config['GOOGLE_DISCOVERY_URL'],
        client_kwargs={'scope': 'openid email profile'},
    )

# Auth routes
@auth_bp.route('/login')
def login():
    redirect_uri = url_for('auth.callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@auth_bp.route('/callback')
def callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get('userinfo')
    
    if not user_info:
        return jsonify({'status': 'error', 'message': 'Failed to get user info'}), 400
    
    if Config.ALLOWED_EMAILS and user_info['email'] not in Config.ALLOWED_EMAILS:
        return jsonify({'status': 'error', 'message': 'Unauthorized email address'}), 403
    
    session['user_info'] = user_info
    
    user = User(
        id=user_info['sub'],
        email=user_info['email'],
        name=user_info['name'],
        picture=user_info.get('picture', '')
    )
    login_user(user)
    
    return redirect('/')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    session.pop('user_info', None)
    return jsonify({'status': 'success', 'message': 'Logged out successfully'})

@auth_bp.route('/user')
def user_info():
    if current_user.is_authenticated:
        return jsonify({
            'status': 'success',
            'user': {
                'id': current_user.id,
                'email': current_user.email,
                'name': current_user.name,
                'picture': current_user.picture,
                'authenticated': True
            }
        })
    
    return jsonify({
        'status': 'error',
        'message': 'Not authenticated',
        'user': {'authenticated': False}
    }), 401

# API routes
@api_bp.route('/')
def index():
    return jsonify({
        'status': 'success',
        'message': 'Android TV Control API',
        'endpoints': [
            '/api/tv/power',
            '/api/tv/volume/up',
            '/api/tv/volume/down',
            '/api/tv/volume/mute',
            '/api/tv/app/launch',
            '/api/tv/navigate'
        ]
    })

@api_bp.route('/tv/power', methods=['POST'])
@login_required
def power():
    controller = get_tv_controller()
    if not controller:
        return jsonify({
            'status': 'error',
            'message': 'Failed to connect to Android TV'
        }), 500
    
    success = controller.power()
    
    if success:
        return jsonify({
            'status': 'success',
            'message': 'Power command sent successfully'
        })
    
    return jsonify({
        'status': 'error',
        'message': 'Failed to send power command'
    }), 500

@api_bp.route('/tv/volume/up', methods=['POST'])
@login_required
def volume_up():
    controller = get_tv_controller()
    if not controller:
        return jsonify({
            'status': 'error',
            'message': 'Failed to connect to Android TV'
        }), 500
    
    success = controller.volume_up()
    
    if success:
        return jsonify({
            'status': 'success',
            'message': 'Volume up command sent successfully'
        })
    
    return jsonify({
        'status': 'error',
        'message': 'Failed to send volume up command'
    }), 500

@api_bp.route('/tv/volume/down', methods=['POST'])
@login_required
def volume_down():
    controller = get_tv_controller()
    if not controller:
        return jsonify({
            'status': 'error',
            'message': 'Failed to connect to Android TV'
        }), 500
    
    success = controller.volume_down()
    
    if success:
        return jsonify({
            'status': 'success',
            'message': 'Volume down command sent successfully'
        })
    
    return jsonify({
        'status': 'error',
        'message': 'Failed to send volume down command'
    }), 500

@api_bp.route('/tv/volume/mute', methods=['POST'])
@login_required
def volume_mute():
    controller = get_tv_controller()
    if not controller:
        return jsonify({
            'status': 'error',
            'message': 'Failed to connect to Android TV'
        }), 500
    
    success = controller.mute()
    
    if success:
        return jsonify({
            'status': 'success',
            'message': 'Mute command sent successfully'
        })
    
    return jsonify({
        'status': 'error',
        'message': 'Failed to send mute command'
    }), 500

@api_bp.route('/tv/app/launch', methods=['POST'])
@login_required
def launch_app():
    data = request.get_json()
    if not data or 'package_name' not in data:
        return jsonify({
            'status': 'error',
            'message': 'Missing package_name parameter'
        }), 400
    
    package_name = data['package_name']
    
    controller = get_tv_controller()
    if not controller:
        return jsonify({
            'status': 'error',
            'message': 'Failed to connect to Android TV'
        }), 500
    
    success = controller.launch_app(package_name)
    
    if success:
        return jsonify({
            'status': 'success',
            'message': f'App {package_name} launched successfully'
        })
    
    return jsonify({
        'status': 'error',
        'message': f'Failed to launch app {package_name}'
    }), 500

@api_bp.route('/tv/navigate', methods=['POST'])
@login_required
def navigate():
    data = request.get_json()
    if not data or 'direction' not in data:
        return jsonify({
            'status': 'error',
            'message': 'Missing direction parameter'
        }), 400
    
    direction = data['direction']
    valid_directions = ['up', 'down', 'left', 'right', 'ok', 'back', 'home']
    
    if direction not in valid_directions:
        return jsonify({
            'status': 'error',
            'message': f'Invalid direction. Must be one of: {", ".join(valid_directions)}'
        }), 400
    
    controller = get_tv_controller()
    if not controller:
        return jsonify({
            'status': 'error',
            'message': 'Failed to connect to Android TV'
        }), 500
    
    success = controller.navigate(direction)
    
    if success:
        return jsonify({
            'status': 'success',
            'message': f'Navigate {direction} command sent successfully'
        })
    
    return jsonify({
        'status': 'error',
        'message': f'Failed to send navigate {direction} command'
    }), 500

# Unauthorized handler
@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
    return redirect(url_for('auth.login'))

def create_app(config_class=Config):
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # Initialize extensions
    CORS(app, supports_credentials=True)
    login_manager.init_app(app)
    
    # Setup OAuth
    setup_oauth(app)
    
    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    
    @app.route('/')
    def index():
        return {
            'status': 'success',
            'message': 'Android TV Control API',
            'version': '1.0.0'
        }
    
    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=app.config['DEBUG'], host='0.0.0.0', port=5000)
