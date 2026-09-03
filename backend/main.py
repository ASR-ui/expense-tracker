from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mysql.connector
from mysql.connector import Error
import bcrypt
from google import genai
import json
import io
import csv
from PIL import Image
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
import secrets
import os

# This loads the key safely from your environment variables
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

app = FastAPI()

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",        # <-- Update if your MySQL username differs
        password="root",    # <-- Update if your MySQL password differs
        database="expense_tracker"
    )

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Users table with premium and password reset columns
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        username VARCHAR(255) PRIMARY KEY, 
        password VARCHAR(255), 
        first_name VARCHAR(255), 
        last_name VARCHAR(255), 
        email VARCHAR(255), 
        phone VARCHAR(50),
        is_premium BOOLEAN DEFAULT FALSE,
        reset_token VARCHAR(255),
        token_expiry BIGINT)''')
    
    # Safe migrations if table already existed
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT FALSE")
    except Exception:
        pass 
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN reset_token VARCHAR(255)")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN token_expiry BIGINT")
    except Exception:
        pass
        
    # 2. Entries table
    cursor.execute('''CREATE TABLE IF NOT EXISTS entries (
        id INT AUTO_INCREMENT PRIMARY KEY, 
        username VARCHAR(255), 
        date VARCHAR(50), 
        type VARCHAR(50), 
        cat VARCHAR(50), 
        amount REAL, 
        description TEXT)''')
        
    # 3. Investments table for Wealth Tracking
    cursor.execute('''CREATE TABLE IF NOT EXISTS investments (
        id INT AUTO_INCREMENT PRIMARY KEY, 
        username VARCHAR(255), 
        asset_name VARCHAR(255), 
        asset_type VARCHAR(50), 
        invested_amount REAL, 
        current_value REAL, 
        date VARCHAR(50))''')

    # 4. Budgets table for Category Spending Caps
    cursor.execute('''CREATE TABLE IF NOT EXISTS budgets (
        username VARCHAR(255), 
        category VARCHAR(50), 
        monthly_limit REAL,
        PRIMARY KEY (username, category))''')
        
    conn.commit()
    conn.close()

init_db()

# --- Email Notification Helper ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your_email@gmail.com"         # <-- Put your Gmail address here
SENDER_PASSWORD = "your_gmail_app_password"  # <-- Put your Google App Password here

def send_email_notification(to_email: str, subject: str, message_body: str):
    if not to_email:
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        msg['Subject'] = subject
        
        msg.attach(MIMEText(message_body, 'html'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, to_email, msg.as_string())
        server.quit()
        print(f"SUCCESS: Email notification sent to {to_email}")
    except Exception as e:
        print("EMAIL ERROR:", e)

# --- Pydantic Data Models ---
class AuthUser(BaseModel):
    username: str
    password: str
    firstName: str = ""
    lastName: str = ""
    email: str = ""
    phone: str = ""

class LedgerEntry(BaseModel):
    date: str
    type: str
    cat: str
    amount: float
    desc: str

class InvestmentEntry(BaseModel):
    asset_name: str
    asset_type: str
    invested_amount: float
    current_value: float
    date: str

class BudgetEntry(BaseModel):
    category: str
    monthly_limit: float

class UPIVerifyRequest(BaseModel):
    username: str
    transaction_id: str = ""

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

# --- Authentication & Account Endpoints ---
@app.post("/api/signup")
def signup(user: AuthUser):
    conn = get_db_connection()
    cursor = conn.cursor()
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(user.password.encode('utf-8'), salt).decode('utf-8')
    
    try:
        cursor.execute(
            "INSERT INTO users (username, password, first_name, last_name, email, phone, is_premium) VALUES (%s, %s, %s, %s, %s, %s, FALSE)", 
            (user.username, hashed_password, user.firstName, user.lastName, user.email, user.phone)
        )
        conn.commit()
        
        # Dispatch Welcome Email Notification
        if user.email:
            first_name = user.firstName or "User"
            subject = "Welcome to Finance Tracker! 📖"
            body = f"""
            <h3>Hello {first_name},</h3>
            <p>Your account has been successfully created with username: <b>{user.username}</b>.</p>
            <p>You can now log in, record your transactions honestly, and manage your personal accounts with paper-and-ink clarity.</p>
            <p>Happy tracking!</p>
            """
            send_email_notification(user.email, subject, body)
            
    except Error as e:
        print("Signup Error:", e)
        raise HTTPException(status_code=400, detail="Username already exists or database error")
    finally:
        conn.close()
        
    return {"message": "User created successfully and welcome email sent"}

@app.post("/api/login")
def login(user: AuthUser):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM users WHERE username = %s", (user.username,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    stored_hash = row[0].encode('utf-8')
    if not bcrypt.checkpw(user.password.encode('utf-8'), stored_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"message": "Login successful"}

@app.post("/api/forgot-password")
def forgot_password(data: ForgotPasswordRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT username, first_name FROM users WHERE email = %s", (data.email,))
        user_row = cursor.fetchone()
        
        if not user_row:
            raise HTTPException(status_code=404, detail="Email address not found")
            
        username = user_row[0]
        first_name = user_row[1] or "User"
        
        # Generate secure token & 15-minute expiry timestamp
        token = secrets.token_urlsafe(32)
        expiry = int(time.time()) + 900
        
        cursor.execute("UPDATE users SET reset_token = %s, token_expiry = %s WHERE email = %s", (token, expiry, data.email))
        conn.commit()
        
        reset_link = f"http://127.0.0.1:8000/index.html?token={token}"
        
        subject = "Password Reset Request - Finance Tracker"
        body = f"""
        <h3>Hello {first_name},</h3>
        <p>We received a request to reset your password for your Finance Tracker account (Username: <b>{username}</b>).</p>
        <p>Click the link below to choose your new password:</p>
        <p><a href="{reset_link}" style="background:#203a24; color:#fff; padding:10px 15px; text-decoration:none; border-radius:4px; display:inline-block;">Reset Password</a></p>
        <p>This link expires in 15 minutes.</p>
        """
        send_email_notification(data.email, subject, body)
        
        return {"status": "success", "message": "Password reset link sent to your email."}
        
    except Error as e:
        print("Forgot Password Error:", e)
        raise HTTPException(status_code=500, detail="Database error during password reset request")
    finally:
        conn.close()

@app.post("/api/reset-password")
def reset_password(data: ResetPasswordRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        current_time = int(time.time())
        cursor.execute("SELECT username FROM users WHERE reset_token = %s AND token_expiry > %s", (data.token, current_time))
        user_row = cursor.fetchone()
        
        if not user_row:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
            
        username = user_row[0]
        
        if len(data.new_password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters long")
            
        salt = bcrypt.gensalt()
        hashed_pw = bcrypt.hashpw(data.new_password.encode('utf-8'), salt).decode('utf-8')
        
        cursor.execute("UPDATE users SET password = %s, reset_token = NULL, token_expiry = NULL WHERE username = %s", (hashed_pw, username))
        conn.commit()
        
        return {"status": "success", "message": "Password successfully updated!"}
        
    except Error as e:
        print("Reset Password Error:", e)
        raise HTTPException(status_code=500, detail="Database error during password update")
    finally:
        conn.close()

@app.delete("/api/users/{username}")
def delete_user(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("DELETE FROM entries WHERE username = %s", (username,))
        cursor.execute("DELETE FROM investments WHERE username = %s", (username,))
        cursor.execute("DELETE FROM budgets WHERE username = %s", (username,))
        
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
            
        return {"status": "success", "message": "User account and all data deleted successfully."}
        
    except Error as e:
        print("Database Error during user deletion:", e)
        raise HTTPException(status_code=500, detail="Failed to delete user account")
    finally:
        conn.close()

# --- Ledger Entries Endpoints ---
@app.get("/api/entries/{username}")
def get_entries(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, date, type, cat, amount, description FROM entries WHERE username = %s", (username,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "date": r[1], "type": r[2], "cat": r[3], "amount": r[4], "desc": r[5]} for r in rows]

@app.post("/api/entries/{username}")
def add_entry(username: str, entry: LedgerEntry):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO entries (username, date, type, cat, amount, description) VALUES (%s, %s, %s, %s, %s, %s)", 
            (username, entry.date, entry.type, entry.cat, entry.amount, entry.desc)
        )
        conn.commit()
        entry_id = cursor.lastrowid
        return {"id": entry_id, "message": "Entry added"}
    except Error as e:
        print("Database Error:", e)
        raise HTTPException(status_code=500, detail="Failed to add entry")
    finally:
        conn.close()

@app.delete("/api/entries/{username}/{entry_id}")
def delete_entry(username: str, entry_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM entries WHERE username = %s AND id = %s", (username, entry_id))
    conn.commit()
    conn.close()
    return {"message": "Entry deleted"}

# --- Investments Endpoints (Wealth Tracker - Premium Gated) ---
@app.get("/api/investments/{username}")
def get_investments(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    cursor.execute("SELECT id, asset_name, asset_type, invested_amount, current_value, date FROM investments WHERE username = %s", (username,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "asset_name": r[1], "asset_type": r[2], "invested_amount": r[3], "current_value": r[4], "date": r[5]} for r in rows]

@app.post("/api/investments/{username}")
def add_investment(username: str, entry: InvestmentEntry):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    try:
        cursor.execute(
            "INSERT INTO investments (username, asset_name, asset_type, invested_amount, current_value, date) VALUES (%s, %s, %s, %s, %s, %s)", 
            (username, entry.asset_name, entry.asset_type, entry.invested_amount, entry.current_value, entry.date)
        )
        conn.commit()
        inv_id = cursor.lastrowid
        return {"id": inv_id, "message": "Investment added"}
    except Error as e:
        print("Database Error:", e)
        raise HTTPException(status_code=500, detail="Failed to add investment")
    finally:
        conn.close()

@app.delete("/api/investments/{username}/{inv_id}")
def delete_investment(username: str, inv_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    cursor.execute("DELETE FROM investments WHERE username = %s AND id = %s", (username, inv_id))
    conn.commit()
    conn.close()
    return {"message": "Investment deleted"}

# --- Budget Endpoints (Premium Gated) ---
@app.get("/api/budgets/{username}")
def get_budgets(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    cursor.execute("SELECT category, monthly_limit FROM budgets WHERE username = %s", (username,))
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}

@app.post("/api/budgets/{username}")
def set_budget(username: str, entry: BudgetEntry):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    try:
        cursor.execute(
            "INSERT INTO budgets (username, category, monthly_limit) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE monthly_limit = %s",
            (username, entry.category, entry.monthly_limit, entry.monthly_limit)
        )
        conn.commit()
        return {"message": "Budget saved successfully"}
    except Error as e:
        print("Database Error:", e)
        raise HTTPException(status_code=500, detail="Failed to save budget")
    finally:
        conn.close()

@app.delete("/api/budgets/{username}/{category}")
def delete_budget(username: str, category: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    try:
        cursor.execute("DELETE FROM budgets WHERE username = %s AND category = %s", (username, category))
        conn.commit()
        return {"message": "Budget limit removed"}
    except Error as e:
        print("Database Error:", e)
        raise HTTPException(status_code=500, detail="Failed to delete budget")
    finally:
        conn.close()

# --- AI Insights & Paywall Endpoints ---
@app.get("/api/insights/{username}")
def get_ai_insights(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    
    if not user_row or not user_row[0]:
        conn.close()
        return {"locked": True, "html": ""}  

    cursor.execute("SELECT date, cat, amount, description FROM entries WHERE username = %s", (username,))
    rows = cursor.fetchall()
    conn.close()
    
    if len(rows) < 3:
        return {"locked": False, "html": "<div class='suggest-item'><p>Keep adding more expenses! I need a bit more data to find patterns.</p></div>"}
        
    ledger_text = "\n".join([f"Category: {r[1]}, Amount: ₹{r[2]}, Desc: {r[3]}" for r in rows])
    
    prompt = f"""
    You are a personal financial advisor. Review this user's ledger and provide 4 actionable tips covering investments, savings boosts, health/wellness expenses, and emergency funds.
    Format your response EXACTLY as HTML blocks like this, with no markdown backticks:
    <div class="suggest-item"><span class="tag">CATEGORY</span><p>Your advice...</p></div>
    
    Ledger:
    {ledger_text}
    """
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt
        )
        clean_html = response.text.replace("```html", "").replace("```", "").strip()
        return {"locked": False, "html": clean_html}
    except Exception as e:
        print("GEMINI ERROR:", e)
        return {"locked": False, "html": "<div class='suggest-item'><p>AI analysis temporarily unavailable.</p></div>"}

# --- Manual UPI Payment Verification Endpoint (With Email Notification) ---
@app.post("/api/verify-upi-payment")
def verify_upi_payment(data: UPIVerifyRequest):
    if not data.username:
        raise HTTPException(status_code=400, detail="Username is required")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT email, first_name FROM users WHERE username = %s", (data.username,))
        user_row = cursor.fetchone()
        
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
            
        user_email = user_row[0]
        first_name = user_row[1] or "User"

        cursor.execute("UPDATE users SET is_premium = TRUE WHERE username = %s", (data.username,))
        conn.commit()
        
        if user_email:
            subject = "⭐ Welcome to Finance Tracker Premium!"
            body = f"""
            <h3>Hello {first_name},</h3>
            <p>Your UPI payment has been successfully verified, and your account has been upgraded to <b>Premium</b>!</p>
            <p>You now have full access to AI Insights, Wealth Tracking, Receipt Scanners, and CSV Exports.</p>
            """
            send_email_notification(user_email, subject, body)
            
        return {"status": "success", "message": "Payment verified and confirmation email sent!"}
        
    except Error as e:
        print("Database Error:", e)
        raise HTTPException(status_code=500, detail="Database error during verification")
    finally:
        conn.close()

# --- AI Vision OCR Receipt Scanner (Premium Gated) ---
@app.post("/api/scan-receipt/{username}")
async def scan_receipt(username: str, file: UploadFile = File(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    conn.close()
    
    if not user_row or not user_row[0]:
        raise HTTPException(status_code=403, detail="Premium feature locked. Payment required.")

    try:
        image_data = await file.read()
        image = Image.open(io.BytesIO(image_data))
        
        prompt = """
        Analyze this receipt and extract the transaction details. 
        Return EXACTLY a JSON object (no markdown, no backticks) with these exact keys:
        - "date": Date of transaction in YYYY-MM-DD format. If you can't find it, return today's date.
        - "amount": Total amount as a float (e.g. 15.50). Do not include currency symbols.
        - "cat": Best matching category from this exact list: Food, Transport, Housing, Utilities, Health, Shopping, Entertainment, Other.
        - "desc": A short description of the purchase (e.g. "Starbucks Coffee" or "Grocery run").
        """
        
        response = ai_client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=[image, prompt]
        )
        
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        parsed_data = json.loads(clean_json)
        
        return parsed_data
        
    except Exception as e:
        print("OCR Error:", e)
        raise HTTPException(status_code=500, detail="Failed to scan receipt")

# --- CSV Data Export (Premium Gated) ---
@app.get("/api/export/{username}")
def export_ledger(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium FROM users WHERE username = %s", (username,))
    user_row = cursor.fetchone()
    
    if not user_row or not user_row[0]:
        conn.close()
        raise HTTPException(status_code=403, detail="Premium feature locked.")

    cursor.execute("SELECT date, type, cat, amount, description FROM entries WHERE username = %s ORDER BY date DESC", (username,))
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["Date", "Type", "Category", "Amount", "Description"])
    for row in rows:
        writer.writerow(row)
    
    output.seek(0)
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={username}_ledger.csv"}
    )