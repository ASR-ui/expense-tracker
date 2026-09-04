import os
import hashlib
import secrets
from datetime import date, datetime, timedelta
from typing import List, Optional

import google.generativeai as genai
import mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import BaseModel

app = FastAPI(title="Expense Tracker API")

# Enable CORS for frontend integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure Gemini AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Aiven MySQL Connection Pool Configuration
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 18832))
DB_USER = os.getenv("DB_USER", "avnadmin")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "defaultdb")

db_pool = None

def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = pooling.MySQLConnectionPool(
            pool_name="expense_pool",
            pool_size=5,
            pool_reset_session=True,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            ssl_disabled=False,
        )
    return db_pool

def get_db_connection():
    try:
        pool = get_db_pool()
        return pool.get_connection()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database connection error: {str(e)}"
        )

# Email Configuration (fastapi-mail via Gmail SMTP)
mail_config = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
    MAIL_FROM=os.getenv("MAIL_FROM", os.getenv("MAIL_USERNAME", "noreply@expensetracker.com")),
    MAIL_PORT=587,
    MAIL_SERVER="smtp.gmail.com",
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=True,
)

async def send_welcome_email(recipient_email: str, username: str):
    if not os.getenv("MAIL_USERNAME") or not os.getenv("MAIL_PASSWORD"):
        return
    message = MessageSchema(
        subject="Welcome to Expense Tracker!",
        recipients=[recipient_email],
        body=f"Hi {username},\n\nYour Expense Tracker account has been created successfully!\n\nBest regards,\nExpense Tracker Team",
        subtype=MessageType.plain
    )
    fm = FastMail(mail_config)
    await fm.send_message(message)

async def send_reset_email(recipient_email: str, token: str):
    if not os.getenv("MAIL_USERNAME") or not os.getenv("MAIL_PASSWORD"):
        return
    reset_link = f"https://expense-tracker-frontend-qnr6.onrender.com?token={token}"
    message = MessageSchema(
        subject="Password Reset - Expense Tracker",
        recipients=[recipient_email],
        body=(
            f"Hello,\n\n"
            f"You requested a password reset for your Expense Tracker account.\n"
            f"Click the link below to set a new password:\n\n"
            f"{reset_link}\n\n"
            f"This link expires in 15 minutes. If you did not make this request, you can safely ignore this email.\n"
        ),
        subtype=MessageType.plain
    )
    fm = FastMail(mail_config)
    await fm.send_message(message)

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

# Initialize Database Schema
@app.on_event("startup")
def setup_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Users Table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            first_name VARCHAR(100),
            last_name VARCHAR(100),
            email VARCHAR(255) UNIQUE NOT NULL,
            phone_number VARCHAR(20),
            username VARCHAR(100) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL
        )
        """
    )

    # Expenses Table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT,
            title VARCHAR(255) NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            category VARCHAR(100) NOT NULL,
            date DATE NOT NULL,
            notes TEXT
        )
        """
    )

    # Password Reset Tokens Table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS password_resets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            token VARCHAR(255) NOT NULL,
            expires_at DATETIME NOT NULL
        )
        """
    )

    # Budgets Table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            category VARCHAR(100) NOT NULL,
            monthly_limit DECIMAL(10, 2) NOT NULL,
            UNIQUE KEY unique_user_category (user_id, category)
        )
        """
    )

    conn.commit()
    cursor.close()
    conn.close()

# Pydantic Schemas
class UserSignup(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str
    phone_number: Optional[str] = None
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

class ExpenseCreate(BaseModel):
    title: str
    amount: float
    category: str
    date: date
    notes: Optional[str] = None
    username: Optional[str] = None

class ExpenseResponse(BaseModel):
    id: int
    title: str
    amount: float
    category: str
    date: date
    notes: Optional[str] = None

class ReceiptAnalysisRequest(BaseModel):
    receipt_text: str

# Health Check
@app.get("/")
def read_root():
    return {"status": "online", "message": "Expense Tracker API is running"}

# User Authentication Endpoints
@app.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(user: UserSignup, background_tasks: BackgroundTasks):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT id FROM users WHERE username = %s OR email = %s", (user.username, user.email))
    existing = cursor.fetchone()
    if existing:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Username or email already exists")

    hashed_pw = hash_password(user.password)
    cursor.execute(
        """
        INSERT INTO users (first_name, last_name, email, phone_number, username, password)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user.first_name, user.last_name, user.email, user.phone_number, user.username, hashed_pw)
    )
    conn.commit()
    user_id = cursor.lastrowid
    cursor.close()
    conn.close()

    background_tasks.add_task(send_welcome_email, user.email, user.username)
    return {"message": "Account created successfully", "user_id": user_id, "username": user.username}

@app.post("/login")
def login(creds: UserLogin):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    hashed_pw = hash_password(creds.password)

    cursor.execute(
        "SELECT id, username, first_name, email FROM users WHERE username = %s AND password = %s",
        (creds.username, hashed_pw)
    )
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return {"message": "Login successful", "user": user}

# Password Reset Flow
@app.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, background_tasks: BackgroundTasks):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT id FROM users WHERE email = %s", (payload.email,))
    user = cursor.fetchone()
    
    if not user:
        cursor.close()
        conn.close()
        return {"message": "If that email is registered, a password reset link has been sent."}
    
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=15)
    
    cursor.execute(
        "INSERT INTO password_resets (email, token, expires_at) VALUES (%s, %s, %s)",
        (payload.email, token, expires_at)
    )
    conn.commit()
    cursor.close()
    conn.close()
    
    background_tasks.add_task(send_reset_email, payload.email, token)
    return {"message": "If that email is registered, a password reset link has been sent."}

@app.post("/reset-password")
def reset_password(payload: ResetPasswordRequest):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute(
        "SELECT email FROM password_resets WHERE token = %s AND expires_at > %s",
        (payload.token, datetime.utcnow())
    )
    record = cursor.fetchone()
    
    if not record:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    
    email = record["email"]
    new_hashed_pw = hash_password(payload.new_password)
    
    cursor.execute("UPDATE users SET password = %s WHERE email = %s", (new_hashed_pw, email))
    cursor.execute("DELETE FROM password_resets WHERE token = %s", (payload.token,))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return {"message": "Password successfully reset! Please log in with your new credentials."}

# User-Specific Data Endpoints (Dashboard & Ledger)
@app.get("/expenses/{username}", response_model=List[ExpenseResponse])
def get_user_expenses(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT e.id, e.title, e.amount, e.category, e.date, e.notes 
        FROM expenses e
        JOIN users u ON e.user_id = u.id
        WHERE u.username = %s
        ORDER BY e.date DESC
        """,
        (username,)
    )
    expenses = cursor.fetchall()
    cursor.close()
    conn.close()
    return expenses

@app.get("/budgets/{username}")
def get_user_budgets(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT b.category, b.monthly_limit
        FROM budgets b
        JOIN users u ON b.user_id = u.id
        WHERE u.username = %s
        """,
        (username,)
    )
    budgets = cursor.fetchall()
    cursor.close()
    conn.close()
    return budgets

@app.post("/budgets/{username}")
def save_user_budget(username: str, payload: dict):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    
    user_id = user["id"]
    category = payload.get("category")
    monthly_limit = payload.get("monthly_limit")

    cursor.execute(
        """
        INSERT INTO budgets (user_id, category, monthly_limit)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE monthly_limit = %s
        """,
        (user_id, category, monthly_limit, monthly_limit)
    )
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Budget saved successfully"}

# General Expense Endpoints
@app.get("/expenses", response_model=List[ExpenseResponse])
def list_expenses():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, title, amount, category, date, notes FROM expenses ORDER BY date DESC")
    expenses = cursor.fetchall()
    cursor.close()
    conn.close()
    return expenses

@app.post("/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def create_expense(expense: ExpenseCreate):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    user_id = None
    if expense.username:
        cursor.execute("SELECT id FROM users WHERE username = %s", (expense.username,))
        user = cursor.fetchone()
        if user:
            user_id = user["id"]

    cursor.execute(
        "INSERT INTO expenses (user_id, title, amount, category, date, notes) VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, expense.title, expense.amount, expense.category, expense.date, expense.notes)
    )
    conn.commit()
    expense_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return {
        "id": expense_id,
        "title": expense.title,
        "amount": expense.amount,
        "category": expense.category,
        "date": expense.date,
        "notes": expense.notes
    }

@app.delete("/expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense(expense_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = %s", (expense_id,))
    conn.commit()
    deleted = cursor.rowcount
    cursor.close()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Expense not found")
    return None

# AI Receipt Analysis
@app.post("/analyze-receipt")
def analyze_receipt(payload: ReceiptAnalysisRequest):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Gemini API Key is not set")
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Extract expense details from the following receipt or transaction text. "
            "Return JSON with keys: title, amount (float), category, and date (YYYY-MM-DD).\n\n"
            f"Text: {payload.receipt_text}"
        )
        response = model.generate_content(prompt)
        return {"result": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))