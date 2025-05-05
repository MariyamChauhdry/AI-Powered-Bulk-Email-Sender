from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from email.message import EmailMessage
import smtplib
import os
import re
import traceback
import uuid
import pandas as pd
from dotenv import load_dotenv
from groq import Groq
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# Load Firebase credentials
cred = credentials.Certificate("firebase_config.json")  # Ensure correct path
firebase_admin.initialize_app(cred)  # Initialize Firebase

# Initialize Firestore database
db = firestore.client()

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret_key")

# Initialize clients
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Configuration
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Track files
SENT_LOGS = "sent_emails.log"
OPENED_LOGS = "opened_emails.log"

def log_sent_email(email_id, recipient, subject):
    """Logs email details to Firestore."""
    try:
        doc_ref = db.collection("sent_emails").document(email_id)
        doc_ref.set({
            "email_id": email_id,
            "recipient": recipient,
            "subject": subject,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        print(f"Email logged to Firestore: {recipient}")
    except Exception as e:
        print(f" Error logging sent email: {e}")


def log_opened_email(email_id):
    """Logs email open status to Firestore."""
    try:
        if not email_id or email_id == "{email_id}":  # Ensure valid email_id
            print(" Error: Invalid email_id received for tracking!")
            return

        doc_ref = db.collection("opened_emails").document(email_id)
        doc_ref.set({
            "email_id": email_id,
            "opened_timestamp": firestore.SERVER_TIMESTAMP
        })
        print(f"Email open logged: {email_id}")
    except Exception as e:
        print(f"Error logging opened email: {e}")


def generate_email_content(prompt, email_id):
    try:
        response = client.chat.completions.create(
            model="mixtral-8x7b-32768",
            messages=[
                  {"role": "system", "content": """You have to create short emails."""},
                  {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=500
        )
        content = response.choices[0].message.content
        content = content.replace('\n\n', '<br><br>')  # Double newlines to paragraph breaks
        content = content.replace('\n', '<br>')  

        tracking_url = f"http://localhost:5000/track/{email_id}"
        return f"""
                <div style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto;">
                  <p style="color: #333333; margin-bottom: 15px;">{content}</p>
                  <div style="border-top: 1px solid #eeeeee; margin-top: 20px; padding-top: 15px;">
                   <p style="font-size: 0.9em; color: #666666;">
                     {os.getenv("SMTP_USER")}<br>
                     Sent via Email Automation System
                   </p>
                   </div>
                 <img src='{tracking_url}' width="1" height="1" style="display:none">
                 
                </div>
"""
      

    except Exception as e:
        return f"<p>Error generating content: {e}</p>"
    
# Function to extract emails from an Excel file
def extract_emails(file_path):
    """Extracts emails from Excel and logs them in Firestore."""
    try:
        df = pd.read_excel(file_path, dtype=str, engine='openpyxl')

        df.columns = df.columns.str.strip().str.lower()
        if 'email' not in df.columns:
            print("❌ No 'email' column found")
            return []

        emails = df['email'].str.strip().str.lower().dropna().unique().tolist()

        # Save emails to Firestore
        batch = db.batch()
        for email in emails:
            doc_ref = db.collection("email_recipients").document(email)
            batch.set(doc_ref, {"email": email, "timestamp": firestore.SERVER_TIMESTAMP})

        batch.commit()
        print(f"📩 Emails saved to Firestore: {emails}")
        return emails

    except Exception as e:
        print(f"🔥 Error extracting emails: {e}")
        traceback.print_exc()
        return []


    
# In track_email route
@app.route('/track/<email_id>')
def track_email(email_id):
    try:
        print(f"Tracking request: {email_id}")
        # Validate UUID format
        uuid.UUID(email_id, version=4)
        log_opened_email(email_id)
        return send_from_directory('static', 'pixel.png')
    except ValueError:
        print("Invalid email_id format")
        return send_from_directory('static', 'pixel.png')
    except Exception as e:
        print(f"Tracking error: {str(e)}")
        return send_from_directory('static', 'pixel.png')

def send_individual_email(recipient, subject, content, attachment_path=None):
    email_id = str(uuid.uuid4())
    
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(content, subtype='html')

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            file_data = f.read()
            msg.add_attachment(file_data, maintype="application", subtype="octet-stream", filename=os.path.basename(attachment_path))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
            server.send_message(msg)

        log_sent_email(email_id, recipient, subject)

        # Save status to Firestore
        db.collection("email_status").document(email_id).set({
            "recipient": recipient,
            "subject": subject,
            "status": "Sent",
            "timestamp": firestore.SERVER_TIMESTAMP
        })

        return True
    except Exception as e:
        print(f"🔥 Error sending to {recipient}: {e}")

        # Log failure in Firestore
        db.collection("email_status").document(email_id).set({
            "recipient": recipient,
            "subject": subject,
            "status": f"Failed: {str(e)}",
            "timestamp": firestore.SERVER_TIMESTAMP
        })

        return False


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/send', methods=['POST'])
def send_emails():
    # Handle file uploads
    email_file = request.files.get('email_file')
    attachment = request.files.get('attachment')
    subject = request.form.get('subject', '')
    prompt = request.form.get('prompt', '')
    recipient_type = request.form.get('recipient_type', 'single')

    # Validate inputs
    if not subject or not prompt:
        flash("Subject and prompt are required!", "error")
        return redirect(url_for('index'))

    # Process recipients
    recipients = []
    if recipient_type == 'single':
        recipients = [request.form.get('single_email')]
    elif recipient_type == 'multiple' and email_file:
        try:
            file_path = os.path.join(UPLOAD_FOLDER, email_file.filename)
            print(f"💾 Saving file to: {file_path}")
            email_file.save(file_path)
            print("✅ File saved successfully")
            
            recipients = extract_emails(file_path)
            print(f"📨 Final recipient list: {recipients}")
            
            if not recipients:
                flash("No valid emails found in the file!", "error")
                return redirect(url_for('index'))
            
        except Exception as e:
            print(f"🔥 File processing error: {str(e)}")
            traceback.print_exc()
            flash("Error processing uploaded file", "error")
            return redirect(url_for('index'))

    # Process attachment
    attachment_path = None
    if attachment and attachment.filename:
        attachment_path = os.path.join(UPLOAD_FOLDER, attachment.filename)
        attachment.save(attachment_path)

    # Send emails
    success_count = 0
    for recipient in recipients:
     email_id = str(uuid.uuid4())  # Generate a new email ID
     content = generate_email_content(prompt, email_id)

     if send_individual_email(recipient, subject, content, attachment_path):
        success_count += 1 # Increment success count


    flash(f"Successfully sent {success_count}/{len(recipients)} emails!", "success")
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)