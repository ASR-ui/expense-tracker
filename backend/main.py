import os
from datetime import date
from typing import List, Optional

import google.generativeai as genai
import mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
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
            ssl_disabled=False,  # Enforces SSL required by Aiven
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

# Initialize Database Schema
@app.on_event("startup")
def setup_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            category VARCHAR(100) NOT NULL,
            date DATE NOT NULL,
            notes TEXT
        )
        """
    )
    conn.commit()
    cursor.close()
    conn.close()

# Pydantic Schemas
class ExpenseCreate(BaseModel):
    title: str
    amount: float
    category: str
    date: date
    notes: Optional[str] = None

class ExpenseResponse(ExpenseCreate):
    id: int

class ReceiptAnalysisRequest(BaseModel):
    receipt_text: str

# Endpoints
@app.get("/")
def read_root():
    return {"status": "online", "message": "Expense Tracker API is running"}

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
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (title, amount, category, date, notes) VALUES (%s, %s, %s, %s, %s)",
        (expense.title, expense.amount, expense.category, expense.date, expense.notes)
    )
    conn.commit()
    expense_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return {**expense.dict(), "id": expense_id}

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