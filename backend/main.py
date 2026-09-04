import os
import io
import base64
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import List, Optional, Any, Dict

import httpx
import mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException, status, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import BaseModel

# Database Connection Pool Configuration
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
            connection_timeout=10,
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

# Email Setup (FastAPI-Mail via SMTP)
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
        body=f"Hi {username},\n\nYour account has been created successfully!\n\nBest regards,\nExpense Tracker Team",
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

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                first_name VARCHAR(100),
                last_name VARCHAR(100),
                email VARCHAR(255) UNIQUE NOT NULL,
                phone_number VARCHAR(20),
                username VARCHAR(100) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                is_premium BOOLEAN DEFAULT FALSE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                title VARCHAR(255) NOT NULL,
                amount DECIMAL(10, 2) NOT NULL,
                category VARCHAR(100) NOT NULL,
                date DATE NOT NULL,
                notes TEXT,
                type VARCHAR(20) DEFAULT 'expense'
            )
            """
        )

        for col_sql in [
            "ALTER TABLE expenses ADD COLUMN user_id INT",
            "ALTER TABLE expenses ADD COLUMN type VARCHAR(20) DEFAULT 'expense'",
            "ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT FALSE"
        ]:
            try:
                cursor.execute(col_sql)
                conn.commit()
            except Exception:
                pass

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

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS investments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                asset_name VARCHAR(150) NOT NULL,
                asset_type VARCHAR(100) NOT NULL,
                invested_amount DECIMAL(12, 2) NOT NULL,
                current_value DECIMAL(12, 2) NOT NULL,
                date DATE NOT NULL
            )
            """
        )

        conn.commit()
        cursor.close()
        conn.close()
        print("Database schema verified.")
    except Exception as ex:
        print(f"Database setup note: {ex}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Expense Tracker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request Models
class UserSignup(BaseModel):
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    email: str
    phone: Optional[str] = None
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

@app.get("/")
def read_root():
    return {"status": "online", "message": "Expense Tracker API is running"}

# User Authentication Endpoints
@app.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(user: UserSignup, background_tasks: BackgroundTasks):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE username = %s OR email = %s", (user.username, user.email))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Username or email already exists")

    hashed_pw = hash_password(user.password)
    cursor.execute(
        """
        INSERT INTO users (first_name, last_name, email, phone_number, username, password)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user.firstName, user.lastName, user.email, user.phone, user.username, hashed_pw)
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

@app.delete("/users/{username}")
def delete_account(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    
    uid = user["id"]
    cursor.execute("DELETE FROM expenses WHERE user_id = %s", (uid,))
    cursor.execute("DELETE FROM budgets WHERE user_id = %s", (uid,))
    cursor.execute("DELETE FROM investments WHERE user_id = %s", (uid,))
    cursor.execute("DELETE FROM users WHERE id = %s", (uid,))
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Account deleted"}

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
    
    new_hashed_pw = hash_password(payload.new_password)
    cursor.execute("UPDATE users SET password = %s WHERE email = %s", (new_hashed_pw, record["email"]))
    cursor.execute("DELETE FROM password_resets WHERE token = %s", (payload.token,))
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Password successfully reset! Please log in with your new credentials."}

# Ledger / Entries Endpoints
@app.get("/entries/{username}")
def get_user_entries(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT 
            e.id, 
            e.title, 
            COALESCE(e.category, 'Other') AS cat,
            COALESCE(e.category, 'Other') AS category,
            COALESCE(e.notes, e.title, '') AS `desc`,
            COALESCE(e.notes, e.title, '') AS description,
            CAST(e.amount AS FLOAT) AS amount, 
            DATE_FORMAT(e.date, '%Y-%m-%d') AS date, 
            LOWER(COALESCE(e.type, 'expense')) AS type
        FROM expenses e
        JOIN users u ON e.user_id = u.id
        WHERE u.username = %s
        ORDER BY e.date DESC, e.id DESC
        """,
        (username,)
    )
    entries = cursor.fetchall()
    cursor.close()
    conn.close()
    return entries

@app.post("/entries/{username}", status_code=status.HTTP_201_CREATED)
def create_user_entry(username: str, payload: Dict[str, Any]):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    
    desc = payload.get("desc") or payload.get("description") or payload.get("title") or "Transaction"
    cat = payload.get("cat") or payload.get("category") or "Other"
    amount = float(payload.get("amount", 0.0))
    entry_date = payload.get("date") or str(date.today())
    entry_type = (payload.get("type") or "expense").lower()

    cursor.execute(
        """
        INSERT INTO expenses (user_id, title, amount, category, date, notes, type)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (user["id"], desc, amount, cat, entry_date, desc, entry_type)
    )
    conn.commit()
    entry_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return {"id": entry_id, "desc": desc, "cat": cat, "amount": amount, "date": str(entry_date), "type": entry_type}

@app.delete("/entries/{username}/{entry_id}")
def delete_user_entry(username: str, entry_id: int):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "DELETE e FROM expenses e JOIN users u ON e.user_id = u.id WHERE u.username = %s AND e.id = %s",
        (username, entry_id)
    )
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Entry deleted"}

# Budgets Endpoints
@app.get("/budgets/{username}")
def get_user_budgets(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT b.category, CAST(b.monthly_limit AS FLOAT) AS monthly_limit FROM budgets b JOIN users u ON b.user_id = u.id WHERE u.username = %s",
        (username,)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {r["category"]: r["monthly_limit"] for r in rows}

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
    
    cursor.execute(
        """
        INSERT INTO budgets (user_id, category, monthly_limit) VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE monthly_limit = %s
        """,
        (user["id"], payload.get("category"), payload.get("monthly_limit"), payload.get("monthly_limit"))
    )
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Budget saved"}

@app.delete("/budgets/{username}/{category}")
def delete_user_budget(username: str, category: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "DELETE b FROM budgets b JOIN users u ON b.user_id = u.id WHERE u.username = %s AND b.category = %s",
        (username, category)
    )
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Budget deleted"}

# Wealth / Investments Endpoints
@app.get("/investments/{username}")
def get_user_investments(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT i.id, i.asset_name, i.asset_type, 
               CAST(i.invested_amount AS FLOAT) as invested_amount,
               CAST(i.current_value AS FLOAT) as current_value,
               DATE_FORMAT(i.date, '%Y-%m-%d') as date
        FROM investments i JOIN users u ON i.user_id = u.id
        WHERE u.username = %s ORDER BY i.date DESC
        """,
        (username,)
    )
    investments = cursor.fetchall()
    cursor.close()
    conn.close()
    return investments

@app.post("/investments/{username}")
def save_user_investment(username: str, payload: dict):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    
    cursor.execute(
        """
        INSERT INTO investments (user_id, asset_name, asset_type, invested_amount, current_value, date)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            user["id"], payload.get("asset_name"), payload.get("asset_type"),
            float(payload.get("invested_amount", 0.0)), float(payload.get("current_value", 0.0)),
            payload.get("date") or str(date.today())
        )
    )
    conn.commit()
    inv_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return {"id": inv_id, "message": "Investment saved"}

@app.delete("/investments/{username}/{inv_id}")
def delete_user_investment(username: str, inv_id: int):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "DELETE i FROM investments i JOIN users u ON i.user_id = u.id WHERE u.username = %s AND i.id = %s",
        (username, inv_id)
    )
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Investment deleted"}

# Insights & Pro Verification Endpoints
@app.get("/insights/{username}")
def get_user_insights(username: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    is_premium = bool(user["is_premium"]) if (user and user.get("is_premium") is not None) else True
    html_tips = (
        '<div class="suggest-item"><span class="tag">AI TIP</span><p>Your expenses are well balanced. Keep allocating 20% toward savings.</p></div>'
        '<div class="suggest-item"><span class="tag">BUDGET</span><p>Consider setting a cap on Food & Shopping to maximize savings.</p></div>'
    )
    return {"locked": not is_premium, "html": html_tips}

@app.post("/verify-upi-payment")
def verify_payment(payload: dict):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("UPDATE users SET is_premium = TRUE WHERE username = %s", (payload.get("username"),))
    conn.commit()
    cursor.close()
    conn.close()
    return {"message": "Pro features unlocked"}

# CSV Export Endpoint
@app.get("/export/{username}")
def export_csv(username: str):
    entries = get_user_entries(username)
    csv_lines = ["Date,Type,Category,Amount,Description"]
    for e in entries:
        csv_lines.append(f'{e["date"]},{e["type"]},{e["cat"]},{e["amount"]},"{e["desc"]}"')
    return PlainTextResponse(
        "\n".join(csv_lines),
        headers={"Content-Disposition": f'attachment; filename="ledger_{username}.csv"'}
    )

# Gemini Receipt Scanner Endpoint (REST integration)
@app.post("/scan-receipt/{username}")
async def scan_receipt(username: str, file: UploadFile = File(...)):
    raw_key = os.getenv("GEMINI_API_KEY", "")
    clean_key = raw_key.strip().strip('"').strip("'")
    if not clean_key:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY environment variable is not configured on Render."
        )

    try:
        contents = await file.read()
        mime_type = file.content_type or "image/jpeg"
        base64_data = base64.b64encode(contents).decode("utf-8")

        prompt = (
            "Analyze this receipt image carefully. Extract the transaction date, total amount, category, and a short description. "
            "Respond strictly in raw JSON format with these exact keys: "
            "\"amount\" (number, e.g. 240.50), "
            "\"date\" (string in YYYY-MM-DD format), "
            "\"desc\" (short name of vendor or item), "
            "\"cat\" (must be one of: Food, Transport, Housing, Utilities, Health, Shopping, Entertainment, Other). "
            "Do not include Markdown backticks or extra text."
        )

        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": base64_data}},
                    {"text": prompt}
                ]
            }]
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={clean_key}"

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

        if resp.status_code != 200:
            err_msg = data.get("error", {}).get("message", resp.text)
            raise HTTPException(status_code=500, detail=f"Gemini API Error: {err_msg}")

        candidates = data.get("candidates", [])
        if not candidates:
            return {"amount": 0.0, "desc": "Receipt", "cat": "Food", "date": str(date.today())}

        raw_text = candidates[0]["content"]["parts"][0]["text"].strip()
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            return {
                "amount": float(parsed.get("amount", 0.0)),
                "date": str(parsed.get("date", str(date.today()))),
                "desc": str(parsed.get("desc", "Scanned Receipt")),
                "cat": str(parsed.get("cat", "Food"))
            }

        return {"amount": 0.0, "desc": "Receipt", "cat": "Food", "date": str(date.today())}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Scan Error: {str(e)}")