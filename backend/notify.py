"""
notify.py
---------
Sends an email to hotel staff whenever the agent successfully creates a
booking hold (via the create_booking_hold tool). This is the human-in-the-
loop step: the AI never finalizes a booking on its own, it reserves
inventory and alerts a human to follow up on payment/confirmation.

Works the same regardless of which channel (WhatsApp, Telegram, or the web
demo UI) triggered the booking -- it's called centrally from wherever
run_agent_turn's trace shows a successful create_booking_hold call.
"""

import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime


def send_booking_email(booking: dict) -> bool:
    """`booking` combines the create_booking_hold tool's input + result."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    staff_email = os.environ.get("HOTEL_STAFF_EMAIL")

    if not all([smtp_user, smtp_pass, staff_email]):
        print("[notify.py] SMTP not configured -- skipping email, check .env")
        return False

    body = f"""
New booking HOLD created via AI agent ({datetime.now().strftime('%d %b %Y, %I:%M %p')})
Channel/session: {booking.get('session_id', 'N/A')}

Guest Name:      {booking.get('guest_name', 'N/A')}
Phone Number:    {booking.get('phone_number', 'N/A')}
Property:        {booking.get('property_id', 'N/A')}
Room Type:       {booking.get('room_type', 'N/A')}
Check-in:        {booking.get('check_in', 'N/A')}
Check-out:       {booking.get('check_out', 'N/A')}
Guests:          {booking.get('num_guests', 'N/A')}
Add-ons:         {booking.get('add_ons', 'None')}
Total Price:     INR {booking.get('total_price_inr', 'N/A')}
Hold ID:         {booking.get('hold_id', 'N/A')}

ACTION NEEDED: This is a HOLD, not a confirmed booking. Please contact the
guest to confirm availability and collect payment.
"""

    msg = MIMEText(body)
    msg["Subject"] = f"New Booking Hold - {booking.get('guest_name', 'Guest')} ({booking.get('property_id', '')})"
    msg["From"] = smtp_user
    msg["To"] = staff_email

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [staff_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[notify.py] Failed to send email: {e}")
        return False


def check_and_notify(session_id: str, trace: list) -> None:
    """Scans an agent turn's trace for a successful create_booking_hold call
    and fires off the staff email if found. Call this after every
    run_agent_turn(), from any channel (web, WhatsApp, Telegram)."""
    for entry in trace:
        if entry.get("tool") == "create_booking_hold" and "hold_id" in entry.get("result", {}):
            booking = {**entry["input"], **entry["result"], "session_id": session_id}
            send_booking_email(booking)
