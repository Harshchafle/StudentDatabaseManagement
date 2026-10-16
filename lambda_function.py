import csv
import io
import json
import logging
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from pypdf import PdfReader
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("STUDENT_TABLE", "StudentRecords")
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".csv"}

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=AWS_REGION)


def normalize_key(value):
    """Normalize field names such as 'Full Name' into 'full_name'."""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def extract_docx_text(file_path):
    """Extract paragraphs and table rows from DOCX using only stdlib XML parsing."""
    lines = []
    ns = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    }

    with zipfile.ZipFile(file_path, "r") as docx_zip:
        # Extract paragraphs and tables from the main document XML.
        xml_content = docx_zip.read("word/document.xml")

    root = ET.fromstring(xml_content)

    # Iterate paragraphs; includes paragraphs within table cells too.
    for paragraph in root.findall(".//w:p", ns):
        text_parts = []
        for node in paragraph.findall(".//w:t", ns):
            text_parts.append(node.text or "")
        paragraph_text = "".join(text_parts).strip()
        if paragraph_text:
            lines.append(paragraph_text)

    return "\n".join(lines)


def extract_text(file_path, extension):
    """Extract text from supported document types without third-party DOCX libraries."""
    if extension == ".pdf":
        reader = PdfReader(file_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if extension == ".docx":
        return extract_docx_text(file_path)

    if extension == ".txt":
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as file:
            return file.read()

    if extension == ".csv":
        with open(file_path, "r", encoding="utf-8-sig", newline="", errors="replace") as file:
            sample = file.read()
        rows = list(csv.reader(io.StringIO(sample)))
        lines = []
        for row in rows:
            if not row:
                continue
            if len(row) >= 2:
                lines.append(f"{row[0].strip()}: {', '.join(cell.strip() for cell in row[1:])}")
            else:
                lines.append(row[0].strip())
        return "\n".join(lines)

    raise ValueError(f"Unsupported file extension: {extension}")


def parse_fields(text):
    """
    Parse lines formatted as:
      student_id: CDU2026001
      email: student@rknec.edu
      full_name: Example Student
    Field names are normalized; numbered internship fields are retained.
    """
    fields = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.upper() == "END OF STUDENT PROFILE":
            continue
        if ":" not in line:
            continue
        raw_key, raw_value = line.split(":", 1)
        key = normalize_key(raw_key)
        value = raw_value.strip()
        if key:
            fields[key] = value
    return fields


def to_number_if_valid(value):
    """Convert numeric-looking values to numbers; keep other values as strings."""
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value or value.upper() == "NA":
        return value
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def get_upload_email_from_key(object_key):
    """Get the email folder from student-documents/email/file.ext."""
    parts = object_key.split("/")
    if len(parts) >= 3 and parts[0] == "student-documents":
        return unquote_plus(parts[1]).strip().lower()
    return None


def process_s3_record(record):
    bucket = record["s3"]["bucket"]["name"]
    object_key = unquote_plus(record["s3"]["object"]["key"])
    size = record["s3"]["object"].get("size")

    logger.info("Processing s3://%s/%s", bucket, object_key)

    if not object_key.startswith("student-documents/"):
        logger.info("Skipping object outside student-documents/ prefix: %s", object_key)
        return

    extension = os.path.splitext(object_key)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        logger.warning("Skipping unsupported extension: %s", extension)
        return

    if size is not None and size > MAX_FILE_SIZE:
        raise ValueError(f"File exceeds the {MAX_FILE_SIZE // (1024 * 1024)} MB limit")

    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = os.path.join(temp_dir, os.path.basename(object_key) or "upload")
        s3.download_file(bucket, object_key, local_path)
        text = extract_text(local_path, extension)

    fields = parse_fields(text)
    logger.info("Extracted field names: %s", sorted(fields.keys()))

    email = fields.get("email", "").strip().lower()
    if not email:
        raise ValueError("Document does not contain a non-empty 'email: ...' field")

    if not re.fullmatch(r"[a-z0-9._%+-]+@rknec\.edu", email):
        raise ValueError("Email must use the @rknec.edu domain")

    upload_email = get_upload_email_from_key(object_key)
    if upload_email and upload_email != email:
        raise ValueError(
            f"Email mismatch: document email '{email}' does not match upload folder '{upload_email}'"
        )

    # DynamoDB table partition key is email (String); this assumes no sort key.
    item = {
        "email": email,
        "student_id": fields.get("student_id", ""),
        "full_name": fields.get("full_name", ""),
        "department": fields.get("department", ""),
        "course_program": fields.get("course_program", ""),
        "entry_route": fields.get("entry_route", ""),
        "graduation_year": to_number_if_valid(fields.get("graduation_year", "")),
        "current_cgpa": to_number_if_valid(fields.get("current_cgpa", "")),
        "class_10_percentage": to_number_if_valid(fields.get("class_10_percentage", "")),
        "class_12_percentage": to_number_if_valid(fields.get("class_12_percentage", "")),
        "diploma_percentage": to_number_if_valid(fields.get("diploma_percentage", "")),
        "active_backlogs": fields.get("active_backlogs", ""),
        "active_backlog_count": to_number_if_valid(fields.get("active_backlog_count", "")),
        "dead_backlogs": fields.get("dead_backlogs", ""),
        "dead_backlog_count": to_number_if_valid(fields.get("dead_backlog_count", "")),
        "technical_skills": fields.get("technical_skills", ""),
        "certifications": fields.get("certifications", ""),
        "awards_achievements": fields.get("awards_achievements", ""),
        "internship_completed": fields.get("internship_completed", ""),
        "internship_count": to_number_if_valid(fields.get("internship_count", "")),
        "source_s3_bucket": bucket,
        "source_s3_key": object_key,
        "processing_status": "PROCESSED",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    for key, value in fields.items():
        if re.fullmatch(r"internship_\d+_[a-z0-9_]+", key):
            item[key] = value

    # Omit optional empty attributes; always retain required partition key.
    item = {
        key: value
        for key, value in item.items()
        if value is not None and (value != "" or key == "email")
    }

    table.put_item(Item=item)
    logger.info("Successfully stored record for email=%s", email)


def lambda_handler(event, context):
    logger.info("Received event: %s", json.dumps(event))

    records = event.get("Records", [])
    if not records:
        logger.warning(
            "No Records found in event. Invoke with an S3 ObjectCreated event to process a document."
        )
        return {
            "statusCode": 200,
            "body": json.dumps("No S3 records to process")
        }

    successes = 0
    failures = []

    for record in records:
        if "s3" not in record:
            logger.warning("Skipping event record without S3 details")
            continue

        try:
            process_s3_record(record)
            successes += 1
        except Exception as exc:
            logger.exception("Failed to process S3 record")
            failures.append({
                "key": record.get("s3", {}).get("object", {}).get("key", "unknown"),
                "error": str(exc)
            })

    if failures:
        raise RuntimeError(f"Failed to process {len(failures)} S3 record(s): {failures}")

    return {
        "statusCode": 200,
        "body": json.dumps({"processed_records": successes})
    }
