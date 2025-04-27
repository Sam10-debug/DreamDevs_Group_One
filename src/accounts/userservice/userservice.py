# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Userservice manages user account creation, user login, and related tasks
"""

import atexit
from datetime import datetime, timedelta
import logging
import os
import sys
import re

import bcrypt
import jwt
from flask import Flask, jsonify, request
import bleach
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from opentelemetry import trace
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.propagate import set_global_textmap
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
from opentelemetry.instrumentation.flask import FlaskInstrumentor

from db import UserDb

# from sendgrid import SendGridAPIClient
# from sendgrid.helpers.mail import Mail

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def create_app():
    """Flask application factory to create instances
    of the Userservice Flask App
    """
    app = Flask(__name__)

    # Disabling unused-variable for lines with route decorated functions
    # as pylint thinks they are unused
    # pylint: disable=unused-variable

    @app.route('/version', methods=['GET'])
    def version():
        """
        Service version endpoint
        """
        return app.config['VERSION'], 200

    @app.route('/ready', methods=['GET'])
    def readiness():
        """
        Readiness probe
        """
        return 'ok', 200

    @app.route('/users', methods=['POST'])
    def create_user():
        """Create a user record.

        Fails if that username already exists.

        Generates a unique accountid.

        request fields:
        - username
        - password
        - password-repeat
        - firstname
        - lastname
        - birthday
        - timezone
        - address
        - state
        - zip
        - ssn
        - email
        """
        try:
            app.logger.debug('Sanitizing input.')
            req = {k: bleach.clean(v) for k, v in request.form.items()}
            __validate_new_user(req)
            # Check if user already exists
            if users_db.get_user(req['username']) is not None:
                raise NameError('user {} already exists'.format(req['username']))

            # Create password hash with salt
            app.logger.debug("Creating password hash.")
            password = req['password']
            salt = bcrypt.gensalt()
            passhash = bcrypt.hashpw(password.encode('utf-8'), salt)

            accountid = users_db.generate_accountid()

            # Create user data to be added to the database
            user_data = {
                'accountid': accountid,
                'username': req['username'],
                'passhash': passhash,
                'firstname': req['firstname'],
                'lastname': req['lastname'],
                'birthday': req['birthday'],
                'timezone': req['timezone'],
                'address': req['address'],
                'state': req['state'],
                'zip': req['zip'],
                'ssn': req['ssn'],
                'email': req['email']
            }
            # Add user_data to database
            app.logger.debug("Adding user to the database")
            users_db.add_user(user_data)
            app.logger.info("Successfully created user.")

            send_email(req['email'], 'Welcome to MoniNext', f'Hello {req["firstname"]}, welcome to MoniNext! Your account has been successfully created.')
            app.logger.info('Welcome email sent to %s', req['email'])

        except UserWarning as warn:
            app.logger.error("Error creating new user: %s", str(warn))
            return str(warn), 400
        except NameError as err:
            app.logger.error("Error creating new user: %s", str(err))
            return str(err), 409
        except SQLAlchemyError as err:
            app.logger.error("Error creating new user: %s", str(err))
            return 'failed to create user', 500

        return jsonify({}), 201

    def __validate_new_user(req):
        app.logger.debug('validating create user request: %s', str(req))
        # Check if required fields are filled
        fields = (
            'username',
            'password',
            'password-repeat',
            'firstname',
            'lastname',
            'birthday',
            'timezone',
            'address',
            'state',
            'zip',
            'ssn',
            'email'
        )
        if any(f not in req for f in fields):
            raise UserWarning('missing required field(s)')
        if any(not bool(req[f] or req[f].strip()) for f in fields):
            raise UserWarning('missing value for input field(s)')

        # Verify username contains only 2-15 alphanumeric or underscore characters
        if not re.match(r"\A[a-zA-Z0-9_]{2,15}\Z", req['username']):
            raise UserWarning('username must contain 2-15 alphanumeric characters or underscores')
        # Check if passwords match
        if not req['password'] == req['password-repeat']:
            raise UserWarning('passwords do not match')

    @app.route('/login', methods=['GET'])
    def login():
        """Login a user and return a JWT token

        Fails if username doesn't exist or password doesn't match hash

        token expiry time determined by environment variable

        request fields:
        - username
        - password
        """
        app.logger.debug('Sanitizing login input.')
        username = bleach.clean(request.args.get('username'))
        password = bleach.clean(request.args.get('password'))

        # Get user data
        try:
            app.logger.debug('Getting the user data.')
            user = users_db.get_user(username)
            if user is None:
                raise LookupError('user {} does not exist'.format(username))

            # Validate the password
            app.logger.debug('Validating the password.')
            if not bcrypt.checkpw(password.encode('utf-8'), user['passhash']):
                raise PermissionError('invalid login')

            full_name = '{} {}'.format(user['firstname'], user['lastname'])
            exp_time = datetime.utcnow() + timedelta(seconds=app.config['EXPIRY_SECONDS'])
            payload = {
                'user': username,
                'acct': user['accountid'],
                'name': full_name,
                'iat': datetime.utcnow(),
                'exp': exp_time,
            }
            app.logger.debug('Creating jwt token.')
            token = jwt.encode(payload, app.config['PRIVATE_KEY'], algorithm='RS256')
            app.logger.info('Login Successful.')

            # Send email notification
            send_email(user['email'], 'Login Notification', f'Hello {full_name}, you have successfully logged in to your account on {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}.')
            app.logger.info('Login notification email sent to %s', user['email'])
            
            return jsonify({'token': token}), 200

        except LookupError as err:
            app.logger.error('Error logging in: %s', str(err))
            return str(err), 404
        except PermissionError as err:
            app.logger.error('Error logging in: %s', str(err))
            return str(err), 401
        except SQLAlchemyError as err:
            app.logger.error('Error logging in: %s', str(err))
            return 'failed to retrieve user information', 500

    @app.route('/users/<accountid>', methods=['GET'])
    def get_user_by_accountid(accountid):
        """Get user data for the specified accountid.

        Params: accountid - the accountid of the user
        Return: a key/value dict of user attributes,
                {'username': username, 'accountid': accountid, ...}
                or None if that user does not exist
        Raises: SQLAlchemyError if there was an issue with the database
        """
        try:
            app.logger.debug('Getting user data.')
            user = users_db.get_user_by_accountid(accountid)
            if user is None:
                raise LookupError('user with accountid {} does not exist'.format(accountid))
            app.logger.info('Successfully retrieved user data.')
            return jsonify(user), 200
        except LookupError as err:
            app.logger.error('Error retrieving user data: %s', str(err))
            return str(err), 404
        except SQLAlchemyError as err:
            app.logger.error('Error retrieving user data: %s', str(err))
            return 'failed to retrieve user information', 500


    def send_email(to_email, subject, content):
        # Configuration (load from environment variables)
        SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
        SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
        SMTP_USER = os.environ.get("SMTP_USER")
        SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

        # Create email
        msg = MIMEMultipart()
        msg["From"] = "noreply@bankofanthos.com"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(content, "html"))

        # Send email
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
            return True
        except Exception as e:
            print(f"Email failed: {str(e)}")
            return False

    @atexit.register
    def _shutdown():
        """Executed when web app is terminated."""
        app.logger.info("Stopping userservice.")

    # Set up logger
    app.logger.handlers = logging.getLogger('gunicorn.error').handlers
    app.logger.setLevel(logging.getLogger('gunicorn.error').level)
    app.logger.info('Starting userservice.')

    # Set up tracing and export spans to Cloud Trace.
    if os.environ['ENABLE_TRACING'] == "true":
        app.logger.info("✅ Tracing enabled.")
        # Set up tracing and export spans to Cloud Trace
        trace.set_tracer_provider(TracerProvider())
        cloud_trace_exporter = CloudTraceSpanExporter()
        trace.get_tracer_provider().add_span_processor(
            BatchSpanProcessor(cloud_trace_exporter)
        )
        set_global_textmap(CloudTraceFormatPropagator())
        FlaskInstrumentor().instrument_app(app)
    else:
        app.logger.info("🚫 Tracing disabled.")

    app.config['VERSION'] = os.environ.get('VERSION')
    app.config['EXPIRY_SECONDS'] = int(os.environ.get('TOKEN_EXPIRY_SECONDS'))
    app.config['PRIVATE_KEY'] = open(os.environ.get('PRIV_KEY_PATH'), 'r').read()
    app.config['PUBLIC_KEY'] = open(os.environ.get('PUB_KEY_PATH'), 'r').read()

    # Configure database connection
    try:
        users_db = UserDb(os.environ.get("ACCOUNTS_DB_URI"), app.logger)
    except OperationalError:
        app.logger.critical("users_db database connection failed")
        sys.exit(1)
    return app


if __name__ == "__main__":
    # Create an instance of flask server when called directly
    USERSERVICE = create_app()
    USERSERVICE.run()
