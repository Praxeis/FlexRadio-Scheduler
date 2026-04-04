from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    callsign = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), default='')
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_approved = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    ban_reason = db.Column(db.String(200), default='')
    monthly_hour_limit = db.Column(db.Integer, default=0)  # 0 = use system default
    zerotier_node_id = db.Column(db.String(10), default='')  # 10-char hex ZeroTier node ID
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    bookings = db.relationship('Booking', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Booking(db.Model):
    __tablename__ = 'bookings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    hour = db.Column(db.Integer, nullable=False)  # 0-23
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('date', 'hour', name='uq_date_hour'),
    )


class BlockedSlot(db.Model):
    __tablename__ = 'blocked_slots'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    hour = db.Column(db.Integer, nullable=False)  # 0-23
    reason = db.Column(db.String(200), default='')  # e.g. "Field Day", "Contest"
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('date', 'hour', name='uq_blocked_date_hour'),
    )


class RadioConfig(db.Model):
    __tablename__ = 'radio_config'

    id = db.Column(db.Integer, primary_key=True)
    connection_mode = db.Column(db.String(20), nullable=False, default='direct')  # 'direct' or 'relay'
    radio_host = db.Column(db.String(100), nullable=False, default='192.168.1.100')
    radio_port = db.Column(db.Integer, nullable=False, default=4992)
    relay_url = db.Column(db.String(200), default='')
    relay_api_key = db.Column(db.String(100), default='')
    enforcement_enabled = db.Column(db.Boolean, default=False)
    # ZeroTier network access control
    zerotier_enabled = db.Column(db.Boolean, default=False)
    zerotier_network_id = db.Column(db.String(16), default='')  # 16-char hex network ID
    zerotier_api_token = db.Column(db.String(200), default='')  # ZeroTier Central API token
    # Maintenance mode
    maintenance_mode = db.Column(db.Boolean, default=False)
    maintenance_message = db.Column(db.String(500), default='')
    maintenance_until = db.Column(db.DateTime, nullable=True)  # optional scheduled end time
    # Monthly hour limits
    default_monthly_hours = db.Column(db.Integer, default=0)  # 0 = unlimited
    # Enforcement tuning
    check_interval = db.Column(db.Integer, default=30)  # seconds between enforcement checks
    # Club branding
    club_logo_filename = db.Column(db.String(200), default='')
    club_website = db.Column(db.String(300), default='')
    # Timezone
    site_timezone = db.Column(db.String(50), default='America/Los_Angeles')
