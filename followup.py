#!/usr/bin/env python3
"""
Automated follow-up email sender.

Reads constraints from config.json, finds sent emails without replies,
and creates draft follow-ups in Gmail.

Setup:
  1. Enable Gmail API: https://console.developers.google.com/
  2. Download OAuth credentials as credentials.json
  3. Run once to authorize: python3 followup.py
  4. Schedule with cron: 0 9 * * * python3 /path/to/followup.py

Config (config.json):
  my_email        - Your Gmail address
  rules           - List of follow-up rules:
    recipients      - List of email addresses to watch (empty = all)
    days_without_reply - Days before sending follow-up
    max_followups   - Max follow-up attempts per thread
    subject_filter  - Only watch emails matching this subject substring
    message_template - The follow-up message body
  dry_run         - If true, prints what would happen without creating drafts
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import getaddresses

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"
CONFIG_FILE = "config.json"
STATE_FILE = ".followup_state.json"


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"ERROR: {CREDENTIALS_FILE} not found.")
                print("Download OAuth credentials from Google Cloud Console:")
                print("  https://console.developers.google.com/")
                print("  APIs & Services > Credentials > Create OAuth 2.0 Client ID")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_thread_messages(service, thread_id):
    thread = service.users().threads().get(userId="me", id=thread_id, format="metadata").execute()
    return thread.get("messages", [])


def get_header(message, name):
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def has_reply_from_recipient(messages, my_email, recipient):
    """Check if the recipient has replied in this thread."""
    for msg in messages:
        from_header = get_header(msg, "from").lower()
        if recipient.lower() in from_header or (
            "@" in recipient and recipient.split("@")[0].lower() in from_header
        ):
            return True
    return False


def thread_followup_count(state, thread_id):
    return state.get(thread_id, {}).get("followup_count", 0)


def mark_followup_sent(state, thread_id):
    if thread_id not in state:
        state[thread_id] = {}
    state[thread_id]["followup_count"] = state[thread_id].get("followup_count", 0) + 1
    state[thread_id]["last_followup"] = datetime.now(timezone.utc).isoformat()


def create_draft_reply(service, thread_id, to, subject, body, dry_run=False):
    # Extract bare email address if in "Name <email>" format
    parsed = getaddresses([to])
    clean_to = parsed[0][1] if parsed and parsed[0][1] else to

    message = MIMEMultipart("alternative")
    message["To"] = clean_to
    message["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject

    text_part = MIMEText(body, "plain")
    message.attach(text_part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    if dry_run:
        print(f"  [DRY RUN] Would create draft reply to {to}")
        print(f"  Subject: {message['Subject']}")
        print(f"  Body preview: {body[:80]}...")
        return None

    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": thread_id}},
    ).execute()
    return draft["id"]


def process_rule(service, config, rule, state, dry_run):
    my_email = config["my_email"]
    days = rule.get("days_without_reply", 3)
    max_followups = rule.get("max_followups", 2)
    recipients = rule.get("recipients", [])
    subject_filter = rule.get("subject_filter", "").lower()
    template = rule.get("message_template", "Just following up on my previous email.")
    rule_name = rule.get("name", "Rule")

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    after_str = cutoff_date.strftime("%Y/%m/%d")

    # Build search query for sent emails older than threshold (but not too old - 90 days max)
    old_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    before_str = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    after_old_str = old_cutoff.strftime("%Y/%m/%d")

    if recipients:
        for recipient in recipients:
            query = f"from:{my_email} to:{recipient} after:{after_old_str} before:{before_str} -in:drafts"
            _scan_sent_emails(
                service, query, my_email, [recipient], subject_filter,
                template, max_followups, state, dry_run, rule_name, days
            )
    else:
        query = f"from:{my_email} after:{after_old_str} before:{before_str} -in:drafts"
        _scan_sent_emails(
            service, query, my_email, [], subject_filter,
            template, max_followups, state, dry_run, rule_name, days
        )


def _scan_sent_emails(service, query, my_email, recipients, subject_filter,
                      template, max_followups, state, dry_run, rule_name, days):
    print(f"\n[{rule_name}] Searching: {query}")

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()
    except HttpError as e:
        print(f"  Gmail API error: {e}")
        return

    messages = results.get("messages", [])
    if not messages:
        print("  No sent emails found matching criteria.")
        return

    print(f"  Found {len(messages)} sent email(s) to check.")

    processed_threads = set()
    drafts_created = 0

    for msg_ref in messages:
        msg_id = msg_ref["id"]

        # Fetch message details
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="metadata"
            ).execute()
        except HttpError:
            continue

        thread_id = msg["threadId"]
        if thread_id in processed_threads:
            continue
        processed_threads.add(thread_id)

        subject = get_header(msg, "subject")
        to_header = get_header(msg, "to")
        date_header = get_header(msg, "date")

        # Subject filter
        if subject_filter and subject_filter not in subject.lower():
            continue

        # Check follow-up count limit
        count = thread_followup_count(state, thread_id)
        if count >= max_followups:
            continue

        # Determine recipient(s) to check
        if recipients:
            check_recipients = recipients
        else:
            # Extract clean email addresses from the To header
            parsed = getaddresses([to_header])
            check_recipients = [addr for _, addr in parsed if addr and "@" in addr]
            # Skip emails to self
            check_recipients = [r for r in check_recipients if my_email.lower() not in r.lower()]
            if not check_recipients:
                continue

        # Get all messages in the thread
        thread_messages = get_thread_messages(service, thread_id)

        for recipient in check_recipients:
            if has_reply_from_recipient(thread_messages, my_email, recipient):
                continue  # Already got a reply from this person

            # No reply — create a follow-up draft
            print(f"  No reply from {recipient} on: \"{subject}\" (thread {thread_id})")

            draft_id = create_draft_reply(
                service, thread_id, recipient, subject, template, dry_run
            )
            if draft_id:
                print(f"  Draft created (id: {draft_id})")
                mark_followup_sent(state, thread_id)
                save_state(state)  # save after each draft so crashes don't lose progress
                drafts_created += 1
            elif dry_run:
                drafts_created += 1

    print(f"  Drafts created: {drafts_created}")


def main():
    config = load_config()
    state = load_state()
    dry_run = config.get("dry_run", False)

    if dry_run:
        print("=== DRY RUN MODE (no drafts will be created) ===")

    print(f"Account: {config['my_email']}")
    print(f"Rules: {len(config.get('rules', []))}")

    service = get_gmail_service()

    for rule in config.get("rules", []):
        process_rule(service, config, rule, state, dry_run)

    if not dry_run:
        save_state(state)

    print("\nDone.")


if __name__ == "__main__":
    main()
