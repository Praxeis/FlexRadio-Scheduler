import os
import secrets
import time
import threading
import xml.etree.ElementTree as ET
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo

import resend
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

DEFAULT_TZ = 'America/Los_Angeles'


def get_local_tz():
    """Get the configured timezone from RadioConfig, or fall back to default."""
    try:
        config = RadioConfig.query.first()
        if config and config.site_timezone:
            return ZoneInfo(config.site_timezone)
    except Exception:
        pass
    return ZoneInfo(DEFAULT_TZ)

import requests as http_requests
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import db, User, Booking, BlockedSlot, RadioConfig
from radio_monitor import RadioMonitor

MAX_SLOTS_PER_DAY = 2

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///scheduler.db'

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, int(user_id))
    if user and user.is_banned:
        return None  # Forces logout for banned users
    return user


@app.context_processor
def inject_globals():
    config = RadioConfig.query.first()
    local_tz = get_local_tz()
    return dict(
        club_logo_filename=(config.club_logo_filename or '') if config else '',
        club_website=(config.club_website or '') if config else '',
        site_timezone=(config.site_timezone or DEFAULT_TZ) if config else DEFAULT_TZ,
        now_local=datetime.now(local_tz)
    )


with app.app_context():
    db.create_all()

    # Migrate: add new columns if missing (SQLite doesn't auto-add from create_all)
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)

    user_cols = {c['name'] for c in inspector.get_columns('users')}
    for col_name, col_def in [
        ('zerotier_node_id', "VARCHAR(10) DEFAULT ''"),
        ('is_banned', "BOOLEAN DEFAULT 0"),
        ('ban_reason', "VARCHAR(200) DEFAULT ''"),
        ('monthly_hour_limit', "INTEGER DEFAULT 0"),
        ('email', "VARCHAR(200) DEFAULT ''"),
        ('is_approved', "BOOLEAN DEFAULT 1"),
    ]:
        if col_name not in user_cols:
            db.session.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}"))
    db.session.commit()

    rc_cols = {c['name'] for c in inspector.get_columns('radio_config')}
    for col_name, col_def in [
        ('zerotier_enabled', "BOOLEAN DEFAULT 0"),
        ('zerotier_network_id', "VARCHAR(16) DEFAULT ''"),
        ('zerotier_api_token', "VARCHAR(200) DEFAULT ''"),
        ('maintenance_mode', "BOOLEAN DEFAULT 0"),
        ('maintenance_message', "VARCHAR(500) DEFAULT ''"),
        ('maintenance_until', "DATETIME"),
        ('default_monthly_hours', "INTEGER DEFAULT 0"),
        ('check_interval', "INTEGER DEFAULT 30"),
        ('club_logo_filename', "VARCHAR(200) DEFAULT 'carp-logo.png'"),
        ('club_website', "VARCHAR(300) DEFAULT 'https://www.k6arp.org'"),
        ('site_timezone', "VARCHAR(50) DEFAULT 'America/Los_Angeles'"),
        ('resend_api_key', "VARCHAR(200) DEFAULT ''"),
        ('sender_email', "VARCHAR(200) DEFAULT ''"),
    ]:
        if col_name not in rc_cols:
            db.session.execute(text(f"ALTER TABLE radio_config ADD COLUMN {col_name} {col_def}"))
    db.session.commit()

    # Ensure a RadioConfig row exists
    if not RadioConfig.query.first():
        db.session.add(RadioConfig(club_logo_filename='carp-logo.png', club_website='https://www.k6arp.org'))
        db.session.commit()

    # Ensure uploads directory exists and seed logo
    uploads_dir = os.path.join(app.static_folder, 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)
    src_logo = os.path.join(app.static_folder, 'carp-logo.png')
    dst_logo = os.path.join(uploads_dir, 'carp-logo.png')
    if os.path.exists(src_logo) and not os.path.exists(dst_logo):
        import shutil
        shutil.copy2(src_logo, dst_logo)

# Start the radio monitor
radio_monitor = RadioMonitor(app)
radio_monitor.start()


# --- Auth routes ---

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('schedule'))

    if request.method == 'POST':
        callsign = request.form['callsign'].strip().upper()
        name = request.form['name'].strip()
        email = request.form.get('email', '').strip()
        password = request.form['password']
        zerotier_node_id = request.form.get('zerotier_node_id', '').strip().lower()

        if not callsign or not name or not password or not email:
            flash('All fields are required.', 'danger')
            return redirect(url_for('register'))

        # Validate email format
        import re
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash('Please enter a valid email address.', 'danger')
            return redirect(url_for('register'))

        # Validate ZeroTier node ID format if provided
        if zerotier_node_id:
            if not re.match(r'^[0-9a-f]{10}$', zerotier_node_id):
                flash('ZeroTier Node ID must be exactly 10 hex characters.', 'danger')
                return redirect(url_for('register'))

        if User.query.filter_by(callsign=callsign).first():
            flash('That callsign is already registered.', 'danger')
            return redirect(url_for('register'))

        user = User(callsign=callsign, name=name, email=email, zerotier_node_id=zerotier_node_id)
        user.set_password(password)

        # First user becomes admin and is auto-approved
        if User.query.count() == 0:
            user.is_admin = True
            user.is_approved = True

        db.session.add(user)
        db.session.commit()
        login_user(user)
        if user.is_approved:
            flash(f'Welcome, {user.callsign}!', 'success')
        else:
            flash('Registration successful! Your account is pending admin approval.', 'info')
        return redirect(url_for('schedule'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('schedule'))

    if request.method == 'POST':
        callsign = request.form['callsign'].strip().upper()
        password = request.form['password']

        user = User.query.filter_by(callsign=callsign).first()
        if user and user.check_password(password):
            if user.is_banned:
                reason = f' Reason: {user.ban_reason}' if user.ban_reason else ''
                flash(f'Your account has been suspended.{reason}', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            return redirect(request.args.get('next') or url_for('schedule'))

        flash('Invalid callsign or password.', 'danger')
        return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# --- Email helper ---

def send_email(to_email, subject, html_body):
    """Send an email via Resend. Returns True on success, error string on failure."""
    config = RadioConfig.query.first()
    if not config or not config.resend_api_key or not config.sender_email:
        return 'Email is not configured. Ask an admin to set up email in Radio settings.'
    resend.api_key = config.resend_api_key
    try:
        resend.Emails.send({
            "from": config.sender_email,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        })
        return True
    except Exception as e:
        return str(e)


def generate_reset_token(user_id):
    s = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return s.dumps(user_id, salt='password-reset')


def verify_reset_token(token, max_age=3600):
    s = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        user_id = s.loads(token, salt='password-reset', max_age=max_age)
        return user_id
    except (SignatureExpired, BadSignature):
        return None


# --- Password reset routes ---

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('schedule'))

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        # Look up by callsign or email
        user = User.query.filter_by(callsign=identifier.upper()).first()
        if not user:
            user = User.query.filter_by(email=identifier).first()

        if user and user.email:
            token = generate_reset_token(user.id)
            reset_url = url_for('reset_password_token', token=token, _external=True)
            html = render_template('email_reset.html', user=user, reset_url=reset_url)
            result = send_email(user.email, 'Password Reset — FlexRadio Scheduler', html)
            if result is not True:
                flash(f'Could not send email: {result}', 'danger')
                return redirect(url_for('forgot_password'))

        # Always show success to avoid revealing which accounts exist
        flash('If an account with that callsign or email exists, a reset link has been sent.', 'info')
        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password_token(token):
    if current_user.is_authenticated:
        return redirect(url_for('schedule'))

    user_id = verify_reset_token(token)
    if not user_id:
        flash('Invalid or expired reset link. Please request a new one.', 'danger')
        return redirect(url_for('forgot_password'))

    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if len(password) < 4:
            flash('Password must be at least 4 characters.', 'danger')
            return redirect(url_for('reset_password_token', token=token))
        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('reset_password_token', token=token))

        user.set_password(password)
        db.session.commit()
        flash('Password has been reset. You can now log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)


# --- Profile route ---

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        import re
        action = request.form.get('action', 'update_profile')

        if action == 'change_password':
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not current_user.check_password(current_password):
                flash('Current password is incorrect.', 'danger')
                return redirect(url_for('profile'))

            if len(new_password) < 4:
                flash('New password must be at least 4 characters.', 'danger')
                return redirect(url_for('profile'))

            if new_password != confirm_password:
                flash('New passwords do not match.', 'danger')
                return redirect(url_for('profile'))

            current_user.set_password(new_password)
            db.session.commit()
            flash('Password changed successfully.', 'success')
            return redirect(url_for('profile'))

        # Default: update profile fields
        email = request.form.get('email', '').strip()
        if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash('Please enter a valid email address.', 'danger')
            return redirect(url_for('profile'))

        zt_node = request.form.get('zerotier_node_id', '').strip().lower()
        if zt_node and not re.match(r'^[0-9a-f]{10}$', zt_node):
            flash('ZeroTier Node ID must be exactly 10 hex characters.', 'danger')
            return redirect(url_for('profile'))

        current_user.email = email
        current_user.zerotier_node_id = zt_node
        db.session.commit()
        flash('Profile updated.', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html')


# --- Schedule routes ---

@app.route('/')
def index():
    return redirect(url_for('schedule'))


@app.route('/schedule')
@login_required
def schedule():
    # Determine the week to display (Monday-based)
    week_param = request.args.get('week')
    if week_param:
        try:
            week_start = date.fromisoformat(week_param)
        except ValueError:
            week_start = date.today()
    else:
        week_start = date.today()

    # Align to Monday
    week_start = week_start - timedelta(days=week_start.weekday())
    week_end = week_start + timedelta(days=6)
    prev_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)

    days = [week_start + timedelta(days=i) for i in range(7)]
    hours = list(range(24))

    # Fetch bookings for this week
    bookings = Booking.query.filter(
        Booking.date >= week_start,
        Booking.date <= week_end
    ).all()

    # Build lookup: (date, hour) -> booking
    booking_map = {}
    for b in bookings:
        booking_map[(b.date, b.hour)] = b

    # Fetch blocked slots for this week
    blocked_slots = BlockedSlot.query.filter(
        BlockedSlot.date >= week_start,
        BlockedSlot.date <= week_end
    ).all()
    blocked_map = {}
    for bs in blocked_slots:
        blocked_map[(bs.date, bs.hour)] = bs

    # Get maintenance state for template
    config = RadioConfig.query.first()
    is_maintenance = config.maintenance_mode if config else False

    # Get monthly usage for current user
    today = date.today()
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    monthly_used = Booking.query.filter(
        Booking.user_id == current_user.id,
        Booking.date >= month_start,
        Booking.date <= month_end
    ).count()
    user_limit = current_user.monthly_hour_limit
    if user_limit == 0 and config:
        user_limit = config.default_monthly_hours
    # user_limit 0 means unlimited

    now_local = datetime.now(get_local_tz())

    return render_template('schedule.html',
                           days=days, hours=hours,
                           booking_map=booking_map,
                           blocked_map=blocked_map,
                           is_maintenance=is_maintenance,
                           monthly_used=monthly_used,
                           monthly_limit=user_limit,
                           week_start=week_start,
                           prev_week=prev_week,
                           next_week=next_week,
                           today=today,
                           now_local=now_local)


@app.route('/book', methods=['POST'])
@login_required
def book():
    if not current_user.is_approved:
        flash('Your account is pending admin approval.', 'warning')
        return redirect(url_for('schedule'))

    # Check maintenance mode
    config = RadioConfig.query.first()
    if config and config.maintenance_mode:
        flash('Booking is disabled during maintenance.', 'warning')
        return redirect(url_for('schedule'))

    book_date = date.fromisoformat(request.form['date'])
    book_hour = int(request.form['hour'])

    # Check if slot is in the past (Pacific Time)
    now = datetime.now(get_local_tz())
    slot_start = datetime(book_date.year, book_date.month, book_date.day, book_hour, tzinfo=get_local_tz())
    if slot_start <= now:
        flash('Cannot book a time slot in the past.', 'danger')
        return redirect(url_for('schedule', week=book_date.isoformat()))

    # Check if slot is blocked
    blocked = BlockedSlot.query.filter_by(date=book_date, hour=book_hour).first()
    if blocked:
        flash(f'That slot is blocked: {blocked.reason}', 'danger')
        return redirect(url_for('schedule', week=book_date.isoformat()))

    # Check slot not already taken
    existing = Booking.query.filter_by(date=book_date, hour=book_hour).first()
    if existing:
        flash('That slot is already booked.', 'danger')
        return redirect(url_for('schedule', week=book_date.isoformat()))

    # Check daily limit
    day_count = Booking.query.filter_by(user_id=current_user.id, date=book_date).count()
    if day_count >= MAX_SLOTS_PER_DAY:
        flash(f'You can only book {MAX_SLOTS_PER_DAY} slots per day.', 'warning')
        return redirect(url_for('schedule', week=book_date.isoformat()))

    # Check monthly hour limit
    user_limit = current_user.monthly_hour_limit
    if user_limit == 0 and config:
        user_limit = config.default_monthly_hours
    if user_limit > 0:
        month_start = book_date.replace(day=1)
        if book_date.month == 12:
            month_end = book_date.replace(year=book_date.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end = book_date.replace(month=book_date.month + 1, day=1) - timedelta(days=1)
        monthly_count = Booking.query.filter(
            Booking.user_id == current_user.id,
            Booking.date >= month_start,
            Booking.date <= month_end
        ).count()
        if monthly_count >= user_limit:
            flash(f'Monthly hour limit reached ({user_limit} hours). Contact an admin for more.', 'warning')
            return redirect(url_for('schedule', week=book_date.isoformat()))

    booking = Booking(user_id=current_user.id, date=book_date, hour=book_hour)
    db.session.add(booking)
    db.session.commit()
    radio_monitor.check_now()  # Trigger immediate enforcement
    flash(f'Booked {book_date} at {book_hour:02d}:00.', 'success')
    return redirect(url_for('schedule', week=book_date.isoformat()))


@app.route('/cancel/<int:booking_id>', methods=['POST'])
@login_required
def cancel(booking_id):
    if not current_user.is_approved:
        flash('Your account is pending admin approval.', 'warning')
        return redirect(url_for('schedule'))

    booking = db.session.get(Booking, booking_id)
    if not booking:
        flash('Booking not found.', 'danger')
        return redirect(url_for('schedule'))

    if booking.user_id != current_user.id:
        flash('You can only cancel your own bookings.', 'danger')
        return redirect(url_for('schedule'))

    week = booking.date.isoformat()
    db.session.delete(booking)
    db.session.commit()
    radio_monitor.check_now()  # Trigger immediate enforcement
    flash('Booking cancelled.', 'success')
    return redirect(url_for('schedule', week=week))


# --- Admin routes ---

@app.route('/admin')
@login_required
def admin():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    users = User.query.order_by(User.callsign).all()
    bookings = Booking.query.order_by(Booking.date, Booking.hour).all()
    blocked_slots = BlockedSlot.query.filter(
        BlockedSlot.date >= date.today()
    ).order_by(BlockedSlot.date, BlockedSlot.hour).all()
    return render_template('admin.html', users=users, bookings=bookings, blocked_slots=blocked_slots)


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    if user_id == current_user.id:
        flash('You cannot delete yourself.', 'warning')
        return redirect(url_for('admin'))

    user = db.session.get(User, user_id)
    if user:
        db.session.delete(user)
        db.session.commit()
        flash(f'User {user.callsign} deleted.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/toggle_admin/<int:user_id>', methods=['POST'])
@login_required
def toggle_admin(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    if user_id == current_user.id:
        flash('You cannot change your own role.', 'warning')
        return redirect(url_for('admin'))

    user = db.session.get(User, user_id)
    if user:
        user.is_admin = not user.is_admin
        db.session.commit()
        role = 'Admin' if user.is_admin else 'Member'
        flash(f'{user.callsign} is now {role}.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/set_hours/<int:user_id>', methods=['POST'])
@login_required
def set_hours(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    user = db.session.get(User, user_id)
    if user:
        try:
            limit = int(request.form.get('monthly_hour_limit', 0))
            user.monthly_hour_limit = max(0, limit)
            db.session.commit()
            if limit > 0:
                flash(f'{user.callsign} monthly limit set to {limit} hours.', 'success')
            else:
                flash(f'{user.callsign} set to use system default.', 'success')
        except (ValueError, TypeError):
            flash('Invalid hour limit.', 'danger')

    return redirect(url_for('admin'))


@app.route('/admin/cancel/<int:booking_id>', methods=['POST'])
@login_required
def admin_cancel(booking_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    booking = db.session.get(Booking, booking_id)
    if booking:
        db.session.delete(booking)
        db.session.commit()
        radio_monitor.check_now()
        flash('Booking cancelled by admin.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/ban/<int:user_id>', methods=['POST'])
@login_required
def ban_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    if user_id == current_user.id:
        flash('You cannot ban yourself.', 'warning')
        return redirect(url_for('admin'))

    user = db.session.get(User, user_id)
    if user:
        user.is_banned = True
        user.ban_reason = request.form.get('ban_reason', '').strip()
        # Cancel all future bookings for this user
        today = date.today()
        future_bookings = Booking.query.filter(
            Booking.user_id == user_id,
            Booking.date >= today
        ).all()
        for b in future_bookings:
            db.session.delete(b)
        db.session.commit()
        flash(f'{user.callsign} has been banned. {len(future_bookings)} booking(s) cancelled.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/unban/<int:user_id>', methods=['POST'])
@login_required
def unban_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    user = db.session.get(User, user_id)
    if user:
        user.is_banned = False
        user.ban_reason = ''
        db.session.commit()
        flash(f'{user.callsign} has been unbanned.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    user = db.session.get(User, user_id)
    if user:
        user.is_approved = True
        db.session.commit()
        flash(f'{user.callsign} has been approved.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/deny/<int:user_id>', methods=['POST'])
@login_required
def deny_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    user = db.session.get(User, user_id)
    if user:
        callsign = user.callsign
        db.session.delete(user)
        db.session.commit()
        flash(f'{callsign} has been denied and removed.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
def reset_password(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin'))

    new_password = request.form.get('new_password', '')
    if len(new_password) < 4:
        flash('Password must be at least 4 characters.', 'danger')
        return redirect(url_for('admin'))

    user.set_password(new_password)
    db.session.commit()
    flash(f'Password reset for {user.callsign}.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/kick/<int:user_id>', methods=['POST'])
@login_required
def kick_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin'))

    kicked = False
    # Disconnect from radio if connected
    if radio_monitor.client and radio_monitor.client.connected:
        clients = radio_monitor.client.get_clients()
        for client in clients:
            client_call = client.get('callsign', '').upper()
            client_station = client.get('station', '').upper()
            if client_call == user.callsign.upper() or user.callsign.upper() in client_station:
                radio_monitor.client.disconnect_client(client['handle'])
                kicked = True

    # Cancel current hour booking
    now = datetime.now()
    current_booking = Booking.query.filter_by(
        user_id=user_id, date=now.date(), hour=now.hour
    ).first()
    if current_booking:
        db.session.delete(current_booking)
        db.session.commit()

    if kicked:
        flash(f'{user.callsign} has been kicked and disconnected from the radio.', 'success')
    else:
        flash(f'{user.callsign} was not connected. Current booking cancelled if any.', 'info')

    return redirect(url_for('admin'))


# --- Maintenance mode routes ---

@app.route('/admin/maintenance/on', methods=['POST'])
@login_required
def maintenance_on():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    config = RadioConfig.query.first()
    config.maintenance_mode = True
    config.maintenance_message = request.form.get('maintenance_message', '').strip() or 'System is under maintenance.'
    until_str = request.form.get('maintenance_until', '').strip()
    if until_str:
        try:
            config.maintenance_until = datetime.fromisoformat(until_str)
        except ValueError:
            config.maintenance_until = None
    else:
        config.maintenance_until = None
    db.session.commit()
    flash('Maintenance mode enabled.', 'success')
    return redirect(url_for('admin_radio'))


@app.route('/admin/maintenance/off', methods=['POST'])
@login_required
def maintenance_off():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    config = RadioConfig.query.first()
    config.maintenance_mode = False
    config.maintenance_message = ''
    config.maintenance_until = None
    db.session.commit()
    flash('Maintenance mode disabled.', 'success')
    return redirect(url_for('admin_radio'))


# --- Block-out routes ---

@app.route('/admin/block', methods=['POST'])
@login_required
def block_slots():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    start_date = date.fromisoformat(request.form['start_date'])
    end_date = date.fromisoformat(request.form.get('end_date') or request.form['start_date'])
    start_hour = int(request.form.get('start_hour', 0))
    end_hour = int(request.form.get('end_hour', 23))
    reason = request.form.get('reason', '').strip() or 'Blocked'

    if end_date < start_date:
        flash('End date must be on or after start date.', 'danger')
        return redirect(url_for('admin'))

    created = 0
    skipped = 0
    current_date = start_date
    while current_date <= end_date:
        for hour in range(start_hour, end_hour + 1):
            # Skip if already blocked
            existing = BlockedSlot.query.filter_by(date=current_date, hour=hour).first()
            if existing:
                skipped += 1
                continue
            # Remove any existing booking in this slot
            booking = Booking.query.filter_by(date=current_date, hour=hour).first()
            if booking:
                db.session.delete(booking)
            db.session.add(BlockedSlot(
                date=current_date, hour=hour, reason=reason, created_by=current_user.id
            ))
            created += 1
        current_date += timedelta(days=1)

    db.session.commit()
    flash(f'Blocked {created} slot(s). {skipped} already blocked.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/unblock/<int:blocked_id>', methods=['POST'])
@login_required
def unblock_slot(blocked_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    slot = db.session.get(BlockedSlot, blocked_id)
    if slot:
        db.session.delete(slot)
        db.session.commit()
        flash('Slot unblocked.', 'success')

    return redirect(url_for('admin'))


@app.route('/admin/unblock_range', methods=['POST'])
@login_required
def unblock_range():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    reason = request.form.get('reason', '').strip()
    start_date = request.form.get('start_date', '')
    end_date = request.form.get('end_date', '')

    query = BlockedSlot.query
    if reason:
        query = query.filter_by(reason=reason)
    if start_date:
        query = query.filter(BlockedSlot.date >= date.fromisoformat(start_date))
    if end_date:
        query = query.filter(BlockedSlot.date <= date.fromisoformat(end_date))

    count = query.count()
    query.delete()
    db.session.commit()
    flash(f'Removed {count} blocked slot(s).', 'success')
    return redirect(url_for('admin'))


# --- Solar data API ---

_solar_cache = {'data': None, 'fetched_at': 0}
_solar_lock = threading.Lock()
SOLAR_CACHE_TTL = 600  # 10 minutes


def _fetch_solar_data():
    """Fetch solar data from hamqsl.com, cached for 10 minutes."""
    with _solar_lock:
        now = time.time()
        if _solar_cache['data'] and (now - _solar_cache['fetched_at']) < SOLAR_CACHE_TTL:
            return _solar_cache['data']

        try:
            resp = http_requests.get('https://www.hamqsl.com/solarxml.php', timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            solar = root.find('.//solardata')
            if solar is not None:
                data = {
                    'sfi': solar.findtext('solarflux', '?'),
                    'a': solar.findtext('aindex', '?'),
                    'k': solar.findtext('kindex', '?'),
                    'ssn': solar.findtext('sunspots', '?'),
                    'updated': solar.findtext('updated', ''),
                }
                _solar_cache['data'] = data
                _solar_cache['fetched_at'] = now
                return data
        except Exception:
            pass

        return _solar_cache['data'] or {'sfi': '?', 'a': '?', 'k': '?', 'ssn': '?', 'updated': ''}


@app.route('/api/solar')
def solar_data():
    """JSON endpoint for solar propagation data + current operator."""
    data = _fetch_solar_data()
    # Include who's currently scheduled on the radio
    now = datetime.now()
    booking = Booking.query.filter_by(date=now.date(), hour=now.hour).first()
    if booking:
        data = dict(data)  # copy so we don't mutate cache
        data['on_air'] = booking.user.callsign
    else:
        data = dict(data)
        data['on_air'] = None
    return jsonify(data)


# --- Radio status API ---

@app.route('/api/radio_status')
@login_required
def radio_status():
    """JSON endpoint for live radio status, polled by the schedule page."""
    status = radio_monitor.status.copy()
    # Simplify connected_clients for JSON
    status['connected_clients'] = [
        {'callsign': c.get('callsign', '?'), 'station': c.get('station', '?')}
        for c in status.get('connected_clients', [])
    ]
    # Include maintenance state
    config = RadioConfig.query.first()
    if config:
        status['maintenance_mode'] = config.maintenance_mode
        status['maintenance_message'] = config.maintenance_message or ''
    return jsonify(status)


# --- Club branding ---

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico'}


@app.route('/admin/club_branding', methods=['POST'])
@login_required
def club_branding():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    config = RadioConfig.query.first()
    action = request.form.get('action', 'save')

    if action == 'delete_logo':
        if config.club_logo_filename:
            logo_path = os.path.join(app.static_folder, 'uploads', config.club_logo_filename)
            if os.path.exists(logo_path):
                os.remove(logo_path)
            config.club_logo_filename = ''
            db.session.commit()
            flash('Logo removed.', 'success')
        return redirect(url_for('admin_radio'))

    # Handle logo upload
    logo_file = request.files.get('club_logo')
    if logo_file and logo_file.filename:
        from werkzeug.utils import secure_filename
        filename = secure_filename(logo_file.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            flash('Invalid image format. Allowed: PNG, JPG, GIF, SVG, WebP, ICO.', 'danger')
            return redirect(url_for('admin_radio'))
        # Remove old logo if different
        if config.club_logo_filename and config.club_logo_filename != filename:
            old_path = os.path.join(app.static_folder, 'uploads', config.club_logo_filename)
            if os.path.exists(old_path):
                os.remove(old_path)
        logo_file.save(os.path.join(app.static_folder, 'uploads', filename))
        config.club_logo_filename = filename

    # Handle website URL
    config.club_website = request.form.get('club_website', '').strip()

    db.session.commit()
    flash('Club branding updated.', 'success')
    return redirect(url_for('admin_radio'))


# --- Schedule reminders ---

@app.route('/admin/send_reminders', methods=['POST'])
@login_required
def send_reminders():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    local_tz = get_local_tz()
    now_local = datetime.now(local_tz)
    tomorrow_local = now_local + timedelta(hours=24)

    # Find all bookings in the next 24 hours
    today = now_local.date()
    tomorrow_date = tomorrow_local.date()

    bookings = Booking.query.filter(
        Booking.date.between(today, tomorrow_date)
    ).all()

    sent = 0
    errors = 0
    for booking in bookings:
        # Check if this booking is within the next 24 hours
        slot_start = datetime(booking.date.year, booking.date.month, booking.date.day,
                              booking.hour, 0, 0, tzinfo=local_tz)
        if slot_start < now_local or slot_start > tomorrow_local:
            continue

        user = db.session.get(User, booking.user_id)
        if not user or not user.email:
            continue

        time_str = f"{booking.hour:02d}:00 - {booking.hour + 1:02d}:00" if booking.hour < 23 else "23:00 - 00:00"
        date_str = booking.date.strftime('%A, %B %d, %Y')

        html = render_template('email_reminder.html', user=user, date_str=date_str,
                               time_str=time_str, booking=booking)
        result = send_email(user.email, f'Upcoming Booking Reminder — {date_str}', html)
        if result is True:
            sent += 1
        else:
            errors += 1

    if errors:
        flash(f'Sent {sent} reminder(s), {errors} failed.', 'warning')
    elif sent:
        flash(f'Sent {sent} reminder(s) for the next 24 hours.', 'success')
    else:
        flash('No upcoming bookings in the next 24 hours to remind about.', 'info')

    return redirect(url_for('admin'))


# --- Admin radio config ---

@app.route('/admin/radio', methods=['GET', 'POST'])
@login_required
def admin_radio():
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('schedule'))

    config = RadioConfig.query.first()

    if request.method == 'POST':
        config.connection_mode = request.form.get('connection_mode', 'direct')
        config.radio_host = request.form.get('radio_host', '').strip()
        config.radio_port = int(request.form.get('radio_port', 4992))
        config.relay_url = request.form.get('relay_url', '').strip()
        config.relay_api_key = request.form.get('relay_api_key', '').strip()
        config.enforcement_enabled = 'enforcement_enabled' in request.form
        config.zerotier_enabled = 'zerotier_enabled' in request.form
        config.zerotier_network_id = request.form.get('zerotier_network_id', '').strip()
        config.zerotier_api_token = request.form.get('zerotier_api_token', '').strip()
        try:
            config.default_monthly_hours = int(request.form.get('default_monthly_hours', 0))
        except (ValueError, TypeError):
            config.default_monthly_hours = 0
        try:
            config.check_interval = max(5, int(request.form.get('check_interval', 30)))
        except (ValueError, TypeError):
            config.check_interval = 30
        # Timezone
        tz_value = request.form.get('site_timezone', '').strip()
        if tz_value:
            try:
                ZoneInfo(tz_value)  # validate it's a real timezone
                config.site_timezone = tz_value
            except (KeyError, Exception):
                flash('Invalid timezone selected.', 'danger')
        # Email (Resend)
        config.resend_api_key = request.form.get('resend_api_key', '').strip()
        config.sender_email = request.form.get('sender_email', '').strip()
        db.session.commit()

        # Restart monitor connection with new settings
        if radio_monitor.client:
            radio_monitor.client.close()
            radio_monitor.client = None

        flash('Radio configuration saved.', 'success')
        return redirect(url_for('admin_radio'))

    return render_template('admin_radio.html', config=config, status=radio_monitor.status)


if __name__ == '__main__':
    from waitress import serve
    print('Serving on http://0.0.0.0:8080')
    serve(app, host='0.0.0.0', port=8080)
