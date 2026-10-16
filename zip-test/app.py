
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

app = FastAPI(title="University T&P Document Upload")

FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://main.d10h1bfrdc78vs.amplifyapp.com"
).rstrip("/")

AWS_REGION = os.getenv("REGION", "us-east-1")
S3_BUCKET = os.getenv("STUDENT_UPLOAD_BUCKET", "students-s33")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

s3 = boto3.client("s3", region_name=AWS_REGION)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".csv", ".docx"}
MAX_SIZE = 15 * 1024 * 1024  # 15 MB

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
}


@app.get("/")
def home():
    return RedirectResponse(url=FRONTEND_URL, status_code=307)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Student Document API"}


@app.post("/api/upload")
async def upload_file(
    email: str = Form(...),
    file: UploadFile = File(...)
):
    email = email.strip().lower()

    if not re.fullmatch(r"[a-z0-9._%+-]+@rknec\.edu", email):
        raise HTTPException(
            status_code=400,
            detail="Please use a valid university email ending in @rknec.edu."
        )

    filename = Path(file.filename or "document").name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail="Allowed formats: PDF, TXT, CSV and DOCX."
        )

    try:
        content = await file.read(MAX_SIZE + 1)
    finally:
        await file.close()

    if not content:
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is empty."
        )

    if len(content) > MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Maximum file size is 15 MB."
        )

    unique_filename = f"{uuid.uuid4().hex}_{filename}"
    object_key = f"student-documents/{email}/{unique_filename}"

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=object_key,
            Body=content,
            ContentType=CONTENT_TYPES[extension],
            ServerSideEncryption="AES256"
        )
    except (BotoCoreError, ClientError) as exc:
        print(f"S3 upload failed: {exc}")
        raise HTTPException(
            status_code=502,
            detail="Upload to storage failed. Check the backend IAM role and S3 configuration."
        ) from exc

    return {
        "message": "Upload successful",
        "email": email,
        "filename": filename,
        "s3_bucket": S3_BUCKET,
        "s3_key": object_key
    }
