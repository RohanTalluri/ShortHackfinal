from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import os
from datetime import timedelta, datetime, date
import re
import csv
import io
from typing import Optional, Dict, List, Union, cast, Any, TypeVar, ClassVar
import logging
from sqlalchemy import func, Column, Integer, String, Text, Float, Date, DateTime, ForeignKey, desc
from sqlalchemy.orm import relationship, Mapped, mapped_column
import random
import tempfile
import openai
from flask_session import Session

try:
    from config import OPENAI_API_KEY, SECRET_KEY, DATABASE_PATH
except ImportError:
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'your-api-key-here')
    SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-here')
    DATABASE_PATH = os.getenv('DATABASE_PATH', 'instance/samurai.db')

# SQLAlchemy type hints
T = TypeVar('T')

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Ensure the instance folder exists
instance_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
os.makedirs(instance_path, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# Database configuration
db_path = os.path.join(instance_path, DATABASE_PATH)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Session configuration
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

db = SQLAlchemy(app)
Session(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # type: ignore
login_manager.login_message = 'Please login to access this page.'
login_manager.login_message_category = 'error'

# Configure OpenAI
openai.api_key = OPENAI_API_KEY

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default='user')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime)

    def __init__(self, username: str, email: str, password: str) -> None:
        self.username = username.strip()
        self.email = email.strip().lower()
        self.set_password(password)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

class Software(db.Model):
    __tablename__ = 'software'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    vendor: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    license_type: Mapped[str] = mapped_column(String(50), nullable=False)
    total_licenses: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_per_license: Mapped[float] = mapped_column(Float, nullable=False)
    renewal_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    licenses: Mapped[List['License']] = relationship('License', backref='software', lazy=True)
    
    def __init__(self, name: str, vendor: str, description: str, license_type: str,
                 total_licenses: int, cost_per_license: float, renewal_date: date) -> None:
        self.name = name
        self.vendor = vendor
        self.description = description
        self.license_type = license_type
        self.total_licenses = total_licenses
        self.cost_per_license = cost_per_license
        self.renewal_date = renewal_date
    
    @property
    def used_licenses(self) -> int:
        return License.query.filter_by(software_id=self.id, status='active').count()
    
    @property
    def usage_percentage(self) -> float:
        if self.total_licenses == 0:
            return 0
        return (self.used_licenses / self.total_licenses) * 100
    
    @property
    def total_cost(self) -> float:
        return self.total_licenses * self.cost_per_license
    
    @property
    def days_until_renewal(self) -> int:
        return (self.renewal_date - datetime.now().date()).days

class License(db.Model):
    __tablename__ = 'licenses'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    software_id: Mapped[int] = mapped_column(Integer, ForeignKey('software.id'), nullable=False)
    assigned_to: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey('users.id'))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='active')
    assigned_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __init__(self, software_id: int, assigned_to: int, status: str = 'active',
                 assigned_date: Optional[datetime] = None, last_used: Optional[datetime] = None) -> None:
        self.software_id = software_id
        self.assigned_to = assigned_to
        self.status = status
        self.assigned_date = assigned_date or datetime.utcnow()
        self.last_used = last_used

def get_dashboard_stats():
    """Get statistics for the dashboard."""
    try:
        # Get all software
        software_list = Software.query.all()
        
        # Basic stats
        total_software = len(software_list)
        total_licenses = sum(s.total_licenses for s in software_list)
        active_licenses = sum(s.used_licenses for s in software_list)
        total_cost = sum(s.total_licenses * s.cost_per_license for s in software_list)
        
        # License utilization
        utilization = (active_licenses / total_licenses * 100) if total_licenses > 0 else 0
        
        # Compliance metrics
        thirty_days = datetime.now().date() + timedelta(days=30)
        today = datetime.now().date()
        expiring_soon = sum(1 for s in software_list if s.renewal_date <= thirty_days and s.renewal_date > today)
        expired = sum(1 for s in software_list if s.renewal_date <= today)
        
        # Cost metrics
        avg_cost_per_license = total_cost / total_licenses if total_licenses > 0 else 0
        potential_savings = sum(
            (s.total_licenses - s.used_licenses) * s.cost_per_license 
            for s in software_list 
            if s.usage_percentage < 30
        )
        
        # License types distribution
        license_types = {}
        for s in software_list:
            license_types[s.license_type] = license_types.get(s.license_type, 0) + 1
        
        # Vendor distribution
        vendor_distribution = {}
        for s in software_list:
            vendor_distribution[s.vendor] = vendor_distribution.get(s.vendor, 0) + 1
        
        # Usage categories
        high_usage = sum(1 for s in software_list if s.usage_percentage >= 80)
        medium_usage = sum(1 for s in software_list if 30 <= s.usage_percentage < 80)
        low_usage = sum(1 for s in software_list if s.usage_percentage < 30)
        
        # Cost categories
        high_cost = sum(1 for s in software_list if s.cost_per_license * s.total_licenses >= 100000)
        medium_cost = sum(1 for s in software_list if 10000 <= s.cost_per_license * s.total_licenses < 100000)
        low_cost = sum(1 for s in software_list if s.cost_per_license * s.total_licenses < 10000)
        
        return {
            'total_software': total_software,
            'total_licenses': total_licenses,
            'active_licenses': active_licenses,
            'total_cost': total_cost,
            'utilization': utilization,
            'expiring_soon': expiring_soon,
            'expired': expired,
            'avg_cost_per_license': avg_cost_per_license,
            'potential_savings': potential_savings,
            'license_types': license_types,
            'vendor_distribution': vendor_distribution,
            'usage_categories': {
                'high': high_usage,
                'medium': medium_usage,
                'low': low_usage
            },
            'cost_categories': {
                'high': high_cost,
                'medium': medium_cost,
                'low': low_cost
            }
        }
    except Exception as e:
        logger.error(f"Error getting dashboard stats: {e}")
        return {
            'total_software': 0,
            'total_licenses': 0,
            'active_licenses': 0,
            'total_cost': 0,
            'utilization': 0,
            'expiring_soon': 0,
            'expired': 0,
            'avg_cost_per_license': 0,
            'potential_savings': 0,
            'license_types': {},
            'vendor_distribution': {},
            'usage_categories': {'high': 0, 'medium': 0, 'low': 0},
            'cost_categories': {'high': 0, 'medium': 0, 'low': 0}
        }

def get_top_software():
    """Get top software by usage."""
    try:
        software_list = Software.query.all()
        # Sort by usage percentage and get top 3
        sorted_software = sorted(software_list, key=lambda x: x.usage_percentage, reverse=True)
        return sorted_software[:3]
    except Exception as e:
        logger.error(f"Error getting top software: {e}")
        return []

# Add more sample software data
def add_sample_software():
    sample_software = [
        # High-Value Enterprise Software
        Software(
            name='SAP HANA Enterprise',
            vendor='SAP',
            description='In-Memory Database Platform',
            license_type='Per Core',
            total_licenses=48,
            cost_per_license=12000.00,
            renewal_date=datetime.now().date() + timedelta(days=45)
        ),
        Software(
            name='Oracle Cloud Infrastructure',
            vendor='Oracle',
            description='Enterprise Cloud Platform',
            license_type='Per User',
            total_licenses=2000,
            cost_per_license=200.00,
            renewal_date=datetime.now().date() + timedelta(days=15)
        ),
        Software(
            name='Microsoft Azure AD Premium P2',
            vendor='Microsoft',
            description='Advanced Identity Protection',
            license_type='Per User',
            total_licenses=1500,
            cost_per_license=18.00,
            renewal_date=datetime.now().date() + timedelta(days=90)
        ),
        
        # Security Software
        Software(
            name='CrowdStrike Falcon Enterprise',
            vendor='CrowdStrike',
            description='Endpoint Protection Platform',
            license_type='Per Endpoint',
            total_licenses=3000,
            cost_per_license=85.00,
            renewal_date=datetime.now().date() + timedelta(days=10)
        ),
        Software(
            name='Palo Alto Prisma Cloud',
            vendor='Palo Alto Networks',
            description='Cloud Security Platform',
            license_type='Per Workload',
            total_licenses=500,
            cost_per_license=150.00,
            renewal_date=datetime.now().date() + timedelta(days=25)
        ),
        
        # Development Tools
        Software(
            name='JetBrains All Products Pack',
            vendor='JetBrains',
            description='Complete Development Suite',
            license_type='Per User',
            total_licenses=200,
            cost_per_license=649.00,
            renewal_date=datetime.now().date() + timedelta(days=5)
        ),
        Software(
            name='GitHub Enterprise',
            vendor='GitHub',
            description='Enterprise Code Repository',
            license_type='Per User',
            total_licenses=1000,
            cost_per_license=21.00,
            renewal_date=datetime.now().date() + timedelta(days=180)
        ),
        
        # Analytics and BI
        Software(
            name='Snowflake Enterprise',
            vendor='Snowflake',
            description='Data Warehouse Platform',
            license_type='Per Credit',
            total_licenses=5000,
            cost_per_license=23.00,
            renewal_date=datetime.now().date() + timedelta(days=8)
        ),
        Software(
            name='Databricks Unity Catalog',
            vendor='Databricks',
            description='Data Lakehouse Platform',
            license_type='Per DBU',
            total_licenses=10000,
            cost_per_license=15.00,
            renewal_date=datetime.now().date() + timedelta(days=12)
        ),
        
        # Collaboration Tools
        Software(
            name='Miro Enterprise',
            vendor='Miro',
            description='Visual Collaboration Platform',
            license_type='Per User',
            total_licenses=800,
            cost_per_license=16.00,
            renewal_date=datetime.now().date() + timedelta(days=60)
        ),
        Software(
            name='Notion Enterprise',
            vendor='Notion',
            description='Workspace and Wiki Platform',
            license_type='Per User',
            total_licenses=1200,
            cost_per_license=8.00,
            renewal_date=datetime.now().date() + timedelta(days=150)
        ),
        
        # Infrastructure Management
        Software(
            name='HashiCorp Enterprise Suite',
            vendor='HashiCorp',
            description='Infrastructure Automation Suite',
            license_type='Per Node',
            total_licenses=300,
            cost_per_license=200.00,
            renewal_date=datetime.now().date() + timedelta(days=18)
        ),
        Software(
            name='Kubernetes Enterprise Support',
            vendor='VMware',
            description='Container Orchestration Support',
            license_type='Per Cluster',
            total_licenses=50,
            cost_per_license=2000.00,
            renewal_date=datetime.now().date() + timedelta(days=30)
        ),
        
        # Design and Creative
        Software(
            name='Figma Enterprise',
            vendor='Figma',
            description='Design Collaboration Platform',
            license_type='Per Editor',
            total_licenses=150,
            cost_per_license=45.00,
            renewal_date=datetime.now().date() + timedelta(days=75)
        ),
        Software(
            name='AutoCAD Collection',
            vendor='Autodesk',
            description='Complete CAD Suite',
            license_type='Per User',
            total_licenses=100,
            cost_per_license=3295.00,
            renewal_date=datetime.now().date() + timedelta(days=40)
        ),
        
        # Compliance and Security
        Software(
            name='Qualys Enterprise',
            vendor='Qualys',
            description='Vulnerability Management Platform',
            license_type='Per Asset',
            total_licenses=2500,
            cost_per_license=35.00,
            renewal_date=datetime.now().date() + timedelta(days=15)
        ),
        Software(
            name='SailPoint IdentityNow',
            vendor='SailPoint',
            description='Identity Governance Platform',
            license_type='Per Identity',
            total_licenses=3000,
            cost_per_license=25.00,
            renewal_date=datetime.now().date() + timedelta(days=20)
        ),
        
        # Customer Support
        Software(
            name='Zendesk Enterprise Suite',
            vendor='Zendesk',
            description='Customer Service Platform',
            license_type='Per Agent',
            total_licenses=200,
            cost_per_license=199.00,
            renewal_date=datetime.now().date() + timedelta(days=95)
        ),
        Software(
            name='ServiceNow IT Service Management',
            vendor='ServiceNow',
            description='ITSM Platform',
            license_type='Per Fulfiller',
            total_licenses=150,
            cost_per_license=180.00,
            renewal_date=datetime.now().date() + timedelta(days=110)
        )
    ]
    
    # Add existing sample software
    sample_software.extend([
        # Recently Added Software
        Software(
            name='Zoom Enterprise Plus',
            vendor='Zoom',
            description='Advanced Enterprise Video Conferencing',
            license_type='Per Host',
            total_licenses=800,
            cost_per_license=35.00,
            renewal_date=datetime.now().date() + timedelta(days=300)
        ),
        Software(
            name='Microsoft Power BI Pro',
            vendor='Microsoft',
            description='Professional Business Intelligence Tool',
            license_type='Per User',
            total_licenses=500,
            cost_per_license=10.00,
            renewal_date=datetime.now().date() + timedelta(days=250)
        ),
        Software(
            name='AWS EC2 Reserved Instances',
            vendor='Amazon',
            description='Reserved EC2 Compute Instances',
            license_type='Per Instance',
            total_licenses=100,
            cost_per_license=500.00,
            renewal_date=datetime.now().date() + timedelta(days=400)
        ),
        Software(
            name='GitLab Ultimate',
            vendor='GitLab',
            description='Complete DevOps Platform',
            license_type='Per User',
            total_licenses=300,
            cost_per_license=99.00,
            renewal_date=datetime.now().date() + timedelta(days=280)
        ),
        # Expiring Software
        Software(
            name='Cisco Webex Enterprise',
            vendor='Cisco',
            description='Enterprise Collaboration Platform',
            license_type='Per User',
            total_licenses=1000,
            cost_per_license=25.00,
            renewal_date=datetime.now().date() + timedelta(days=15)
        ),
        Software(
            name='Symantec Endpoint Protection',
            vendor='Broadcom',
            description='Enterprise Security Solution',
            license_type='Per Device',
            total_licenses=2000,
            cost_per_license=45.00,
            renewal_date=datetime.now().date() + timedelta(days=20)
        ),
        Software(
            name='Citrix Virtual Apps',
            vendor='Citrix',
            description='Application Virtualization',
            license_type='Per User',
            total_licenses=500,
            cost_per_license=300.00,
            renewal_date=datetime.now().date() + timedelta(days=25)
        ),
        Software(
            name='New Relic Pro',
            vendor='New Relic',
            description='Application Performance Monitoring',
            license_type='Per Host',
            total_licenses=150,
            cost_per_license=75.00,
            renewal_date=datetime.now().date() + timedelta(days=10)
        ),
        # Expired Software
        Software(
            name='Oracle WebLogic Server',
            vendor='Oracle',
            description='Enterprise Application Server',
            license_type='Per Core',
            total_licenses=32,
            cost_per_license=4500.00,
            renewal_date=datetime.now().date() - timedelta(days=15)
        ),
        Software(
            name='IBM Db2',
            vendor='IBM',
            description='Enterprise Database',
            license_type='Per Core',
            total_licenses=16,
            cost_per_license=7800.00,
            renewal_date=datetime.now().date() - timedelta(days=30)
        ),
        Software(
            name='SolarWinds NPM',
            vendor='SolarWinds',
            description='Network Performance Monitor',
            license_type='Per Device',
            total_licenses=100,
            cost_per_license=150.00,
            renewal_date=datetime.now().date() - timedelta(days=5)
        ),
        Software(
            name='Trend Micro Deep Security',
            vendor='Trend Micro',
            description='Server Security Platform',
            license_type='Per Server',
            total_licenses=50,
            cost_per_license=250.00,
            renewal_date=datetime.now().date() - timedelta(days=8)
        ),
        # Actively Used Software
        Software(
            name='Atlassian Confluence',
            vendor='Atlassian',
            description='Team Collaboration Software',
            license_type='Per User',
            total_licenses=1000,
            cost_per_license=5.00,
            renewal_date=datetime.now().date() + timedelta(days=180)
        ),
        Software(
            name='Datadog Enterprise',
            vendor='Datadog',
            description='Infrastructure Monitoring',
            license_type='Per Host',
            total_licenses=200,
            cost_per_license=35.00,
            renewal_date=datetime.now().date() + timedelta(days=150)
        ),
        Software(
            name='PagerDuty Enterprise',
            vendor='PagerDuty',
            description='Incident Management Platform',
            license_type='Per User',
            total_licenses=300,
            cost_per_license=39.00,
            renewal_date=datetime.now().date() + timedelta(days=200)
        ),
        Software(
            name='Okta Enterprise',
            vendor='Okta',
            description='Identity Management',
            license_type='Per User',
            total_licenses=1500,
            cost_per_license=25.00,
            renewal_date=datetime.now().date() + timedelta(days=220)
        )
    ])
    
    return sample_software

def init_db():
    """Initialize the database and create admin user if it doesn't exist."""
    try:
        # Create database directory if it doesn't exist
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # Create all tables
        with app.app_context():
            db.create_all()
            
            # Check if admin user exists
            admin = User.query.filter_by(username='admin').first()
            if not admin:
                # Create admin user
                admin = User(
                    username='admin',
                    email='admin@samurai.com',
                    password='Admin@123'
                )
                admin.role = 'admin'
                db.session.add(admin)
                
                # Add regular users for demo
                demo_users = [
                    User(username='john.doe', email='john.doe@company.com', password='User@123'),
                    User(username='jane.smith', email='jane.smith@company.com', password='User@123'),
                    User(username='bob.wilson', email='bob.wilson@company.com', password='User@123'),
                    User(username='alice.brown', email='alice.brown@company.com', password='User@123'),
                ]
                for user in demo_users:
                    db.session.add(user)
                
                # Add sample software data if none exists
                if Software.query.count() == 0:
                    # Add existing sample software
                    sample_software = add_sample_software()
                    
                    # Add software to database
                    for software in sample_software:
                        db.session.add(software)
                    db.session.commit()
                    
                    # Add sample license assignments
                    users = User.query.all()
                    software_list = Software.query.all()
                    
                    # Create license assignments with varying usage patterns
                    for software in software_list:
                        # Calculate number of licenses to assign (60-90% of total)
                        num_licenses = int(software.total_licenses * random.uniform(0.6, 0.9))
                        
                        for _ in range(num_licenses):
                            user = random.choice(users)
                            license = License(
                                software_id=software.id,
                                assigned_to=user.id,
                                status='active',
                                assigned_date=datetime.now() - timedelta(days=random.randint(1, 180)),
                                last_used=datetime.now() - timedelta(days=random.randint(0, 30))
                            )
                            db.session.add(license)
                    
                    db.session.commit()
                    logger.info("Admin user and sample data created successfully!")
                    logger.info(f"Database initialized at: {db_path}")
                
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        if os.path.exists(db_path):
            logger.info("Removing corrupted database file...")
            os.remove(db_path)
        if 'admin' in locals():
            db.session.rollback()
        raise

@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    if not user_id:
        return None
    try:
        return db.session.get(User, int(user_id))
    except Exception as e:
        logger.error(f"Error loading user: {e}")
        return None

@app.before_request
def before_request():
    session.permanent = True  # Use permanent session
    app.permanent_session_lifetime = timedelta(days=7)  # Set session lifetime

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        
        logger.debug(f"Registration attempt - Username: {username}, Email: {email}")
        
        # Validate all fields are present
        if not username:
            flash('Username is required', 'error')
            return redirect(url_for('register'))
        if not email:
            flash('Email is required', 'error')
            return redirect(url_for('register'))
        if not password:
            flash('Password is required', 'error')
            return redirect(url_for('register'))
        
        # Validate password length
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return redirect(url_for('register'))
        
        try:
            # Check username
            if User.query.filter_by(username=username).first():
                flash('Username already exists', 'error')
                return redirect(url_for('register'))
            
            # Check email
            if User.query.filter_by(email=email.lower()).first():
                flash('Email already registered', 'error')
                return redirect(url_for('register'))
            
            # Create new user
            user = User(
                username=username,
                email=email,
                password=password
            )
            db.session.add(user)
            db.session.commit()
            logger.info(f"New user registered successfully: {username}")
            
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error: {e}")
            flash('An error occurred during registration. Please try again.', 'error')
            return redirect(url_for('register'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = bool(request.form.get('remember'))
        
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        # Get basic stats
        software_list = Software.query.all()
        total_software = len(software_list)
        total_licenses = sum(s.total_licenses for s in software_list)
        used_licenses = sum(s.used_licenses for s in software_list)
        utilization = (used_licenses / total_licenses * 100) if total_licenses > 0 else 0

        # Format stats for display
        stats = {
            'total_software': total_software,
            'total_licenses': total_licenses,
            'used_licenses': used_licenses,
            'utilization': round(utilization, 2)
        }

        # Get top software by usage
        top_software = sorted(software_list, key=lambda x: x.used_licenses, reverse=True)[:5]

        return render_template('dashboard.html', 
                            stats=stats,
                            top_software=top_software)
    except Exception as e:
        logger.error(f"Dashboard error: {str(e)}")
        flash('Error loading dashboard', 'error')
        return render_template('dashboard.html', 
                            stats={'total_software': 0, 'total_licenses': 0, 'used_licenses': 0, 'utilization': 0},
                            top_software=[])

@app.route('/software-inventory')
@login_required
def software_inventory():
    try:
        filter_type = request.args.get('filter', 'all')
        page = request.args.get('page', 1, type=int)
        per_page = 12
        
        logger.info(f"Software inventory accessed by user: {current_user.username}, filter: {filter_type}, page: {page}")
        
        # Get all software first to avoid multiple database queries
        try:
            all_software = Software.query.all()
            if not all_software:
                logger.warning("No software found in database")
                flash('No software found in the inventory.', 'info')
                return render_template('software_inventory.html',
                                    software_list=[],
                                    filter_type=filter_type,
                                    page=1,
                                    per_page=per_page,
                                    total=0,
                                    has_next=False,
                                    has_prev=False,
                                    pages=1)
        except Exception as e:
            logger.error(f"Error querying software: {e}")
            flash('Error accessing software inventory.', 'error')
            return redirect(url_for('dashboard'))
        
        # Get current date for comparisons
        today = datetime.now().date()
        thirty_days = today + timedelta(days=30)
        
        try:
            if filter_type == 'active':
                # Get software with usage > 70%
                filtered_software = [s for s in all_software if s.usage_percentage > 70]
                filtered_software = sorted(filtered_software, key=lambda x: x.usage_percentage, reverse=True)
            elif filter_type == 'expiring':
                # Get software expiring in next 30 days
                filtered_software = [s for s in all_software 
                                   if s.renewal_date <= thirty_days and s.renewal_date > today]
                filtered_software = sorted(filtered_software, key=lambda x: x.renewal_date)
            elif filter_type == 'expired':
                # Get expired software
                filtered_software = [s for s in all_software if s.renewal_date <= today]
                filtered_software = sorted(filtered_software, key=lambda x: x.renewal_date, reverse=True)
            else:
                # For 'all' view, we'll get different categories
                active_software = [s for s in all_software if s.usage_percentage > 70]
                active_software = sorted(active_software, key=lambda x: x.usage_percentage, reverse=True)[:8]
                
                expiring_software = [s for s in all_software 
                                   if s.renewal_date <= thirty_days and s.renewal_date > today]
                expiring_software = sorted(expiring_software, key=lambda x: x.renewal_date)[:8]
                
                expired_software = [s for s in all_software if s.renewal_date <= today]
                expired_software = sorted(expired_software, key=lambda x: x.renewal_date, reverse=True)[:8]
                
                return render_template('software_inventory.html',
                                    active_software=active_software,
                                    expiring_software=expiring_software,
                                    expired_software=expired_software,
                                    filter_type=filter_type,
                                    page=1,
                                    per_page=per_page,
                                    total=len(all_software),
                                    has_next=False,
                                    has_prev=False,
                                    pages=1)
            
            # Manual pagination for filtered results
            total = len(filtered_software)
            start = (page - 1) * per_page
            end = start + per_page
            paginated_software = filtered_software[start:end] if start < total else []
            
            return render_template('software_inventory.html',
                                software_list=paginated_software,
                                filter_type=filter_type,
                                page=page,
                                per_page=per_page,
                                total=total,
                                has_next=end < total,
                                has_prev=page > 1,
                                pages=(total + per_page - 1) // per_page)
                                
        except Exception as e:
            logger.error(f"Error processing software inventory: {e}")
            flash('Error processing software inventory.', 'error')
            return redirect(url_for('dashboard'))
                            
    except Exception as e:
        logger.error(f"Unhandled error in software inventory: {e}")
        flash('An error occurred while loading the software inventory.', 'error')
        return redirect(url_for('dashboard'))

@app.route('/user-management')
@login_required
def user_management():
    page = request.args.get('page', 1, type=int)
    per_page = 10

    # Get users with pagination
    users = User.query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page)

    # Calculate statistics
    total_users = User.query.count()
    admin_users = User.query.filter_by(role='admin').count()
    
    # Users active in last 24 hours
    yesterday = datetime.utcnow() - timedelta(days=1)
    active_users = User.query.filter(User.last_login >= yesterday).count()
    
    # New users this month
    first_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_users = User.query.filter(User.created_at >= first_of_month).count()

    return render_template('user_management.html',
                         users=users,
                         total_users=total_users,
                         admin_users=admin_users,
                         active_users=active_users,
                         new_users=new_users,
                         now=datetime.utcnow(),
                         pages=users.pages,
                         current_page=page,
                         has_prev=users.has_prev,
                         has_next=users.has_next,
                         prev_num=users.prev_num,
                         next_num=users.next_num)

@app.route('/reports')
@login_required
def reports():
    report_type = request.args.get('type', 'general')
    
    # Get all software data
    software_list = Software.query.all()
    
    # Calculate summary statistics
    total_software = len(software_list)
    total_cost = sum(s.total_cost for s in software_list)
    total_licenses = sum(s.total_licenses for s in software_list)
    used_licenses = sum(s.used_licenses for s in software_list)
    avg_usage = sum(s.usage_percentage for s in software_list) / len(software_list) if software_list else 0
    
    # Get expiring licenses
    thirty_days = datetime.now().date() + timedelta(days=30)
    # type: ignore[arg-type]
    expiring_soon = Software.query.filter(Software.renewal_date <= thirty_days).all()
    
    # Get underutilized software (less than 30% usage)
    underutilized = [s for s in software_list if s.usage_percentage < 30]
    
    return render_template('reports.html',
                         report_type=report_type,
                         software_list=software_list,
                         total_software=total_software,
                         total_cost=total_cost,
                         total_licenses=total_licenses,
                         used_licenses=used_licenses,
                         avg_usage=avg_usage,
                         expiring_soon=expiring_soon,
                         underutilized=underutilized)

@app.route('/api/export-report')
@login_required
def export_report():
    try:
        software_list = Software.query.all()
        
        # Create a StringIO object to write CSV data
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write headers
        writer.writerow([
            'Software Name', 'Vendor', 'Total Licenses', 'Used Licenses',
            'Usage %', 'Cost Per License', 'Total Cost', 'Renewal Date',
            'Days Until Renewal'
        ])
        
        # Write data
        for software in software_list:
            writer.writerow([
                software.name,
                software.vendor,
                software.total_licenses,
                software.used_licenses,
                f"{software.usage_percentage:.1f}%",
                f"${software.cost_per_license:,.2f}",
                f"${software.total_cost:,.2f}",
                software.renewal_date.strftime('%Y-%m-%d'),
                software.days_until_renewal
            ])
        
        # Prepare the output
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8')),
            mimetype='text/csv',
            as_attachment=True,
            download_name='SAMurAI_Report.csv'
        )
            
    except Exception as e:
        logger.error(f"Error exporting report: {e}")
        flash('An error occurred while exporting the report.', 'error')
        return redirect(url_for('reports'))

@app.route('/api/users', methods=['POST'])
@login_required
def add_user():
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    
    if not all(key in data for key in ['username', 'email', 'password', 'role']):
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        # Check if username exists
        if User.query.filter_by(username=data['username']).first():
            return jsonify({'error': 'Username already exists'}), 400
        
        # Check if email exists
        if User.query.filter_by(email=data['email']).first():
            return jsonify({'error': 'Email already exists'}), 400
        
        # Create new user
        user = User(
            username=data['username'],
            email=data['email'],
            password=data['password']
        )
        user.role = data['role']
        
        db.session.add(user)
        db.session.commit()
        
        return jsonify({
            'message': 'User created successfully',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'role': user.role
            }
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating user: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/users/<int:user_id>', methods=['PUT'])
@login_required
def update_user(user_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    user = User.query.get_or_404(user_id)
    data = request.get_json()
    
    try:
        if 'username' in data:
            existing = User.query.filter_by(username=data['username']).first()
            if existing and existing.id != user_id:
                return jsonify({'error': 'Username already exists'}), 400
            user.username = data['username']
        
        if 'email' in data:
            existing = User.query.filter_by(email=data['email']).first()
            if existing and existing.id != user_id:
                return jsonify({'error': 'Email already exists'}), 400
            user.email = data['email']
        
        if 'password' in data:
            user.set_password(data['password'])
        
        if 'role' in data:
            # Prevent changing own role or last admin
            if user_id == current_user.id:
                return jsonify({'error': 'Cannot change own role'}), 400
            if user.role == 'admin' and data['role'] != 'admin':
                admin_count = User.query.filter_by(role='admin').count()
                if admin_count <= 1:
                    return jsonify({'error': 'Cannot remove last admin'}), 400
            user.role = data['role']
        
        db.session.commit()
        return jsonify({
            'message': 'User updated successfully',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'role': user.role
            }
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating user: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    if user_id == current_user.id:
        return jsonify({'error': 'Cannot delete own account'}), 400
    
    user = User.query.get_or_404(user_id)
    
    try:
        # Prevent deleting last admin
        if user.role == 'admin':
            admin_count = User.query.filter_by(role='admin').count()
            if admin_count <= 1:
                return jsonify({'error': 'Cannot delete last admin'}), 400
        
        db.session.delete(user)
        db.session.commit()
        return jsonify({'message': 'User deleted successfully'})
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting user: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/software', methods=['POST'])
@login_required
def add_software():
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    
    required_fields = ['name', 'vendor', 'license_type', 'total_licenses', 'cost_per_license', 'renewal_date']
    if not all(key in data for key in required_fields):
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        # Check if software exists
        if Software.query.filter_by(name=data['name'], vendor=data['vendor']).first():
            return jsonify({'error': 'Software already exists'}), 400
        
        # Create new software
        software = Software(
            name=data['name'],
            vendor=data['vendor'],
            description=data.get('description', ''),
            license_type=data['license_type'],
            total_licenses=data['total_licenses'],
            cost_per_license=data['cost_per_license'],
            renewal_date=datetime.strptime(data['renewal_date'], '%Y-%m-%d').date()
        )
        
        db.session.add(software)
        db.session.commit()
        
        return jsonify({
            'message': 'Software added successfully',
            'software': {
                'id': software.id,
                'name': software.name,
                'vendor': software.vendor,
                'description': software.description,
                'license_type': software.license_type,
                'total_licenses': software.total_licenses,
                'cost_per_license': software.cost_per_license,
                'renewal_date': software.renewal_date.strftime('%Y-%m-%d')
            }
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error adding software: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/software/<int:software_id>', methods=['PUT'])
@login_required
def update_software(software_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    software = Software.query.get_or_404(software_id)
    data = request.get_json()
    
    try:
        if 'name' in data:
            existing = Software.query.filter_by(name=data['name'], vendor=software.vendor).first()
            if existing and existing.id != software_id:
                return jsonify({'error': 'Software already exists'}), 400
            software.name = data['name']
        
        if 'vendor' in data:
            existing = Software.query.filter_by(name=software.name, vendor=data['vendor']).first()
            if existing and existing.id != software_id:
                return jsonify({'error': 'Software already exists'}), 400
            software.vendor = data['vendor']
        
        if 'description' in data:
            software.description = data['description']
        
        if 'license_type' in data:
            software.license_type = data['license_type']
        
        if 'total_licenses' in data:
            software.total_licenses = data['total_licenses']
        
        if 'cost_per_license' in data:
            software.cost_per_license = data['cost_per_license']
        
        if 'renewal_date' in data:
            software.renewal_date = datetime.strptime(data['renewal_date'], '%Y-%m-%d').date()
        
        db.session.commit()
        return jsonify({
            'message': 'Software updated successfully',
            'software': {
                'id': software.id,
                'name': software.name,
                'vendor': software.vendor,
                'description': software.description,
                'license_type': software.license_type,
                'total_licenses': software.total_licenses,
                'cost_per_license': software.cost_per_license,
                'renewal_date': software.renewal_date.strftime('%Y-%m-%d')
            }
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating software: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/software/<int:software_id>', methods=['DELETE'])
@login_required
def delete_software(software_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    software = Software.query.get_or_404(software_id)
    
    try:
        # Check if software has active licenses
        active_licenses = License.query.filter_by(software_id=software_id, status='active').count()
        if active_licenses > 0:
            return jsonify({'error': f'Cannot delete software with {active_licenses} active licenses'}), 400
        
        db.session.delete(software)
        db.session.commit()
        return jsonify({'message': 'Software deleted successfully'})
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting software: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/software/search', methods=['GET'])
@login_required
def search_software():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Search query is required'}), 400
    
    try:
        # Search in name, vendor, and description
        software_list = Software.query.filter(
            db.or_(
                cast(Column[str], Software.name).ilike(f'%{query}%'),  # type: ignore[attr-defined]
                cast(Column[str], Software.vendor).ilike(f'%{query}%'),  # type: ignore[attr-defined]
                cast(Column[str], Software.description).ilike(f'%{query}%')  # type: ignore[attr-defined]
            )
        ).all()
        
        return jsonify({
            'results': [{
                'id': s.id,
                'name': s.name,
                'vendor': s.vendor,
                'description': s.description,
                'license_type': s.license_type,
                'total_licenses': s.total_licenses,
                'used_licenses': s.used_licenses,
                'usage_percentage': s.usage_percentage,
                'cost_per_license': s.cost_per_license,
                'total_cost': s.total_cost,
                'renewal_date': s.renewal_date.strftime('%Y-%m-%d'),
                'days_until_renewal': s.days_until_renewal
            } for s in software_list]
        })
        
    except Exception as e:
        logger.error(f"Error searching software: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/ai-dashboard')
@login_required
def ai_dashboard():
    return render_template('ai_dashboard.html')

@app.route('/api/ai-chat', methods=['POST'])
@login_required
def ai_chat():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        message = data.get('message', '')
        if not message:
            return jsonify({'error': 'Message is required'}), 400

        # Get relevant software data
        software_list = Software.query.all()
        
        # Prepare context about software inventory
        context = {
            'total_software': len(software_list),
            'total_licenses': sum(s.total_licenses for s in software_list),
            'used_licenses': sum(s.used_licenses for s in software_list),
            'expiring_soon': sum(1 for s in software_list if s.days_until_renewal <= 30 and s.days_until_renewal > 0),
            'expired': sum(1 for s in software_list if s.days_until_renewal <= 0),
            'high_usage': sum(1 for s in software_list if s.usage_percentage >= 80),
            'low_usage': sum(1 for s in software_list if s.usage_percentage < 30),
            'software_details': [
                {
                    'name': s.name,
                    'vendor': s.vendor,
                    'total_licenses': s.total_licenses,
                    'used_licenses': s.used_licenses,
                    'usage_percentage': s.usage_percentage,
                    'days_until_renewal': s.days_until_renewal,
                    'cost_per_license': s.cost_per_license,
                    'total_cost': s.total_cost
                }
                for s in software_list
            ]
        }

        # Create system message with context
        system_message = f"""You are SAMurAI, an AI assistant for Software Asset Management. 
        Here is the current state of the software assets:
        
        Overview:
        - Total Software: {context['total_software']}
        - Total Licenses: {context['total_licenses']}
        - Used Licenses: {context['used_licenses']}
        - Licenses Expiring in 30 days: {context['expiring_soon']}
        - Expired Licenses: {context['expired']}
        - High Usage Software (>80%): {context['high_usage']}
        - Low Usage Software (<30%): {context['low_usage']}
        
        You have access to detailed information about each software including name, vendor, 
        license counts, usage, costs, and renewal dates. Provide specific, data-driven insights 
        and recommendations based on this information.
        
        When discussing costs, always format them as currency with $ symbol and commas.
        When discussing percentages, always include the % symbol and use one decimal place.
        Be concise but informative in your responses."""

        # Call OpenAI API
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": message}
            ],
            max_tokens=250,
            temperature=0.7
        )

        ai_response = response.choices[0].message.content
        return jsonify({'response': ai_response})

    except Exception as e:
        logger.error(f"Error in AI chat: {e}")
        return jsonify({'error': 'An error occurred processing your request'}), 500

# Initialize database when running the app
if __name__ == '__main__':
    init_db()  # Initialize database and create admin user
    app.run(debug=True) 