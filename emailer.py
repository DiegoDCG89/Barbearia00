"""Envio de e-mail por SMTP — usado só na recuperação de senha.

Variáveis (na Vercel: Settings > Environment Variables):
  SMTP_HOST       servidor (ex.: smtp.gmail.com, smtp-relay.brevo.com, smtp.resend.com)
  SMTP_PORT       587 (STARTTLS, padrão) ou 465 (SSL)
  SMTP_USER       usuário do servidor SMTP
  SMTP_PASSWORD   senha do servidor SMTP (no Gmail: "senha de app", não a senha da conta)
  SMTP_FROM       remetente exibido (padrão: o SMTP_USER)
Sem SMTP_HOST a recuperação por e-mail fica desligada (continua valendo o link enviado pelo
barbeiro no WhatsApp, em Usuários > Link de senha).
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr


def configured():
    return bool(os.environ.get("SMTP_HOST") and (os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")))


def send(to, subject, text, from_name=""):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    user, pwd = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM") or user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, sender)) if from_name else sender
    msg["To"] = to
    msg.set_content(text)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=10, context=ctx) as s:
            if user:
                s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            if user:
                s.login(user, pwd)
            s.send_message(msg)
