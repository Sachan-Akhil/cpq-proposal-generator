import os
import time
import base64
import re
from io import BytesIO
from datetime import datetime

import requests
from flask import Flask, request, jsonify, Response
from requests.auth import HTTPBasicAuth
from openai import OpenAI
import boto3
from botocore.exceptions import NoCredentialsError, ClientError

# REPORTLAB IMPORTS
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, gray, black, white
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph,
    Spacer, ListFlowable, ListItem, PageBreak, Table, TableStyle
)
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

app = Flask(__name__)
client = OpenAI()

# Environment variables
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
if not AWS_REGION or not S3_BUCKET_NAME:
    raise Exception("AWS_REGION and S3_BUCKET_NAME environment variables must be set")

ORACLE_CPQ_USERNAME = os.getenv("ORACLE_CPQ_USERNAME")
ORACLE_CPQ_PASSWORD = os.getenv("ORACLE_CPQ_PASSWORD")
SERVICE_AUTH_USERNAME = os.getenv("API_AUTH_USERNAME")
SERVICE_AUTH_PASSWORD = os.getenv("API_AUTH_PASSWORD")

s3_client = boto3.client('s3', region_name=AWS_REGION)

COLORS = {
    "primary": HexColor("#003366"),    # Navy Blue
    "secondary": HexColor("#0073e6"),  # Bright Blue
    "gray": gray,
    "black": black,
    "white": white,
}

# Basic Auth check
def check_auth():
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        return False
    try:
        encoded = auth.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode('utf-8')
        username, password = decoded.split(":", 1)
    except Exception:
        return False
    return username == SERVICE_AUTH_USERNAME and password == SERVICE_AUTH_PASSWORD

def require_basic_auth(f):
    def decorated(*args, **kwargs):
        if not check_auth():
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated

# Oracle CPQ fetch functions
HEADERS = {"Accept": "application/json"}

def fetch_transaction(base_url, process_var_name, transaction_id):
    api_base = f"https://{base_url}/rest/v19/commerceDocuments{process_var_name}Transaction"
    url = f"{api_base}/{transaction_id}"
    resp = requests.get(url, headers=HEADERS, auth=HTTPBasicAuth(ORACLE_CPQ_USERNAME, ORACLE_CPQ_PASSWORD))
    resp.raise_for_status()
    return resp.json()

def fetch_transaction_lines(base_url, process_var_name, transaction_id):
    api_base = f"https://{base_url}/rest/v19/commerceDocuments{process_var_name}Transaction"
    url = f"{api_base}/{transaction_id}/transactionLine"
    resp = requests.get(url, headers=HEADERS, auth=HTTPBasicAuth(ORACLE_CPQ_USERNAME, ORACLE_CPQ_PASSWORD))
    resp.raise_for_status()
    return resp.json()

# Compose prompt for OpenAI
def extract_string(field_value):
    if isinstance(field_value, str):
        return field_value.strip()
    elif isinstance(field_value, dict):
        for key in ["displayValue", "value", "name"]:
            if key in field_value and isinstance(field_value[key], str):
                return field_value[key].strip()
        return str(field_value)
    elif field_value is None:
        return ""
    else:
        return str(field_value)

def compose_prompt(transaction, transaction_lines):
    customer_name = extract_string(transaction.get("_customer_t_company_name", "Unknown Customer"))
    customer_contact_name = " ".join(filter(None, [
        extract_string(transaction.get("_customer_t_first_name", "")),
        extract_string(transaction.get("_customer_t_last_name", ""))
    ])).strip()

    customer_address_parts = []
    for field in ["_customer_t_address", "_customer_t_city", "_customer_t_state", "_customer_t_zip", "_customer_t_country"]:
        value = extract_string(transaction.get(field, ""))
        if value:
            customer_address_parts.append(value)
    customer_address = ", ".join(customer_address_parts)

    contact_email = extract_string(transaction.get("_customer_t_email", "N/A"))
    contact_phone = extract_string(transaction.get("_customer_t_phone", "N/A"))

    transaction_name = extract_string(transaction.get("transactionName_t", "N/A"))
    total_value = extract_string(transaction.get("totalContractValue_t", "N/A"))
    currency = extract_string(transaction.get("currency_t", "USD"))
    payment_terms = extract_string(transaction.get("paymentTerms_t", "N/A"))
    owner = extract_string(transaction.get("owner_t", "Sales Team"))
    sales_email = extract_string(transaction.get("_owner_email_t", "sales@example.com"))
    sales_phone = extract_string(transaction.get("_owner_phone_t", "N/A"))

    proposal_date = time.strftime("%B %d, %Y")

    lines_text = ""
    for i, line in enumerate(transaction_lines.get("items", []), start=1):
        part_number = extract_string(line.get("_part_number", "N/A"))
        desc = extract_string(line.get("_part_desc")) or extract_string(line.get("displayedItemName_l")) or "N/A"

        qty_raw = line.get("requestedQuantity_l", 1)
        try:
            qty = float(qty_raw)
        except (ValueError, TypeError):
            qty = 1

        price_unit = line.get("_price_unit_price_each", {}).get("value", 0)
        currency_local = line.get("_price_unit_price_each", {}).get("currency", currency)

        lead_time = extract_string(line.get("_part_lead_time", "N/A"))
        shipping_date = extract_string(line.get("oRCL_ERP_RequestShipDate_l", "N/A"))

        line_total = qty * price_unit

        lines_text += (
            f"{i}. Product: {desc} (Part #: {part_number})\n"
            f"   Quantity: {qty}\n"
            f"   Unit Price: {price_unit:.2f} {currency_local}\n"
            f"   Line Total: {line_total:.2f} {currency_local}\n"
            f"   Lead Time: {lead_time}\n"
            f"   Estimated Shipping Date: {shipping_date}\n\n"
        )

    prompt = (
        f"Generate a detailed and professional sales proposal document in markdown format.\n"
        f"Proposal Date: {proposal_date}\n"
        f"Prepared by: {owner}\n"
        f"Sales Representative Contact:\n"
        f"Email: {sales_email}\n"
        f"Phone: {sales_phone}\n\n"
        f"Customer Information:\n"
        f"Name: {customer_name}\n"
        f"Contact Person: {customer_contact_name}\n"
        f"Address: {customer_address}\n"
        f"Email: {contact_email}\n"
        f"Phone: {contact_phone}\n\n"
        f"Transaction Details:\n"
        f"Transaction Name: {transaction_name}\n"
        f"Total Contract Value: {total_value} {currency}\n"
        f"Payment Terms: {payment_terms}\n\n"
        f"Line Items:\n{lines_text}\n"
        f"Please include these sections as markdown headings (using ###):\n"
        f"1. Introduction with appreciation.\n"
        f"2. Summary of offered products and services.\n"
        f"3. Pricing and payment terms.\n"
        f"4. Delivery expectations.\n"
        f"5. Terms and conditions.\n"
        f"6. Next steps and contact info.\n"
        f"Use a professional and persuasive tone suitable for a business proposal.\n"
        f"Use bullet points for lists and **bold** for emphasis."
    )
    return prompt

# OpenAI retry handler
def generate_proposal_with_retry(prompt_text, max_retries=3, backoff=2):
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful sales assistant who writes professional and detailed sales proposals."},
                    {"role": "user", "content": prompt_text}
                ],
                max_tokens=700,
                temperature=0.5,
            )
            return response.choices[0].message.content
        except Exception as e:
            if hasattr(e, "status_code") and e.status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(backoff ** attempt)
                else:
                    return jsonify({"error": "OpenAI API rate limit exceeded. Please try again later."}), 429
            else:
                return jsonify({"error": f"OpenAI API error: {str(e)}"}), 500

# Upload PDF to S3
def upload_pdf_to_s3(pdf_bytes_io, transaction_id):
    pdf_bytes_io.seek(0)
    s3_key = f"proposals/CPO_Proposal_{transaction_id}.pdf"
    try:
        s3_client.upload_fileobj(
            pdf_bytes_io,
            S3_BUCKET_NAME,
            s3_key,
            ExtraArgs={"ContentType": "application/pdf"}
        )
    except NoCredentialsError:
        raise Exception("AWS credentials not found or invalid")
    except ClientError as e:
        raise Exception(f"Failed to upload to S3: {e}")

    if AWS_REGION == "us-east-1":
        url = f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
    else:
        url = f"https://{S3_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"

    return url

# PDF Generation

def register_fonts():
    base_dir = os.path.dirname(__file__)
    font_path = os.path.join(base_dir, "DejaVuSans.ttf")
    if os.path.exists(font_path):
        pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", font_path))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Italic", font_path))
    else:
        print("Warning: DejaVuSans.ttf font not found, defaulting to Helvetica")

def get_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='Title',
        fontName='DejaVuSans-Bold',
        fontSize=26,
        alignment=TA_CENTER,
        spaceAfter=24,
        textColor=COLORS['primary']
    ))
    styles.add(ParagraphStyle(
        name='Heading1',
        fontName='DejaVuSans-Bold',
        fontSize=18,
        textColor=COLORS['primary'],
        spaceBefore=18,
        spaceAfter=12,
        alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        name='BodyText',
        fontName='DejaVuSans',
        fontSize=11,
        leading=15,
        spaceAfter=8,
        alignment=TA_JUSTIFY,
    ))
    return styles

class ProposalDocTemplate(BaseDocTemplate):
    def __init__(self, buffer, **kwargs):
        super().__init__(buffer, pagesize=A4, **kwargs)
        margin = 36
        frame = Frame(margin, margin + 40, A4[0] - 2 * margin, A4[1] - 2 * margin - 60, id='normal')
        self.addPageTemplates([PageTemplate(id='normal', frames=frame, onPage=self.draw_header_footer)])

    def draw_header_footer(self, canvas_obj, doc):
        width, height = A4
        canvas_obj.saveState()

        # Header
        fontname = 'DejaVuSans-Bold' if 'DejaVuSans-Bold' in pdfmetrics.getRegisteredFontNames() else 'Helvetica-Bold'
        canvas_obj.setFont(fontname, 14)
        canvas_obj.setFillColor(COLORS['primary'])
        canvas_obj.drawString(36, height - 50, "Your Company Name")

        fontname = 'DejaVuSans' if 'DejaVuSans' in pdfmetrics.getRegisteredFontNames() else 'Helvetica'
        canvas_obj.setFont(fontname, 10)
        canvas_obj.setFillColor(COLORS['secondary'])
        canvas_obj.drawString(36, height - 65, "Sales Proposal Document")

        # Header line
        canvas_obj.setStrokeColor(COLORS['primary'])
        canvas_obj.setLineWidth(1)
        canvas_obj.line(36, height - 75, width - 36, height - 75)

        # Footer line
        canvas_obj.setStrokeColor(COLORS['primary'])
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(36, 55, width - 36, 55)

        # Footer Text
        fontname = 'DejaVuSans-Italic' if 'DejaVuSans-Italic' in pdfmetrics.getRegisteredFontNames() else 'Helvetica-Oblique'
        canvas_obj.setFont(fontname, 9)
        canvas_obj.setFillColor(COLORS['gray'])
        canvas_obj.drawString(36, 40, "Contact: sales@example.com | +1 555 123 4567 | www.yourcompany.com")

        # Page number right aligned
        canvas_obj.drawRightString(width - 36, 40, f"Page {doc.page}")

        canvas_obj.restoreState()

def markdown_to_flowables(text, styles):
    flowables = []
    lines = text.splitlines()
    buffer_paragraph = []
    bullet_items = []
    in_bullet = False

    def flush_paragraph():
        nonlocal buffer_paragraph
        if not buffer_paragraph:
            return
        paragraph_text = " ".join(buffer_paragraph).strip()
        paragraph_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', paragraph_text)
        para = Paragraph(paragraph_text, styles['BodyText'])
        flowables.append(para)
        flowables.append(Spacer(1, 6))
        buffer_paragraph.clear()

    def flush_bullets():
        nonlocal bullet_items
        if not bullet_items:
            return
        items = [ListItem(Paragraph(re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', item), styles['BodyText'])) for item in bullet_items]
        flowables.append(ListFlowable(items, bulletType='bullet'))
        flowables.append(Spacer(1, 6))
        bullet_items.clear()

    for line in lines:
        line = line.strip()
        if line == "":
            flush_bullets()
            flush_paragraph()
            in_bullet = False
            continue
        if line.startswith("###"):
            flush_bullets()
            flush_paragraph()
            heading = line.lstrip('#').strip()
            flowables.append(Paragraph(heading, styles['Heading1']))
            flowables.append(Spacer(1, 12))
            in_bullet = False
            continue
        if line.startswith("- ") or line.startswith("* "):
            flush_paragraph()
            bullet_items.append(line[2:].strip())
            in_bullet = True
            continue
        if in_bullet:
            flush_bullets()
            in_bullet = False
        buffer_paragraph.append(line)
    flush_bullets()
    flush
