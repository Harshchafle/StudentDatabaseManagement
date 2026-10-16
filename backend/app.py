
import os
import re
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

app = FastAPI(title="University T&P Document Upload")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("STUDENT_UPLOAD_BUCKET", "students-s33")

s3 = boto3.client("s3", region_name=AWS_REGION)

BASE_DIR = Path(__file__).resolve().parent

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
    return FileResponse(BASE_DIR / "index.html")


@app.post("/api/upload")
async def upload_file(
    email: str = Form(...),
    file: UploadFile = File(...)
):
    # Validate the university email on the backend.
    email = email.strip().lower()

    if not re.fullmatch(
        r"[a-z0-9._%+-]+@rknec\.edu",
        email
    ):
        raise HTTPException(
            status_code=400,
            detail="Please use a valid university email ending in @rknec.edu."
        )

    # Validate filename and extension.
    filename = Path(file.filename or "document").name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail="Allowed formats: PDF, TXT, CSV and DOCX."
        )

    # Read with a size limit.
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

    # Keep all uploads under one parent prefix,
    # with a separate folder for each university email.
    unique_filename = f"{uuid.uuid4().hex}_{filename}"

    object_key = (
        f"student-documents/{email}/{unique_filename}"
    )

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
            detail=(
                "S3 upload failed. Check AWS credentials, "
                "bucket name, region and IAM permissions."
            )
        ) from exc

    return {
        "message": "Upload successful",
        "email": email,
        "s3_bucket": S3_BUCKET,
        "s3_key": object_key
    }
