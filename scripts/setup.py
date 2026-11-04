"""Create local secrets without printing them or overwriting an existing config."""
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet
root = Path(__file__).resolve().parents[1]
path = root / '.env'
if path.exists():
    raise SystemExit('.env already exists; edit it manually. No files changed.')
text = (root / '.env.example').read_text()
text = text.replace('ADMIN_TOKEN=\n', 'ADMIN_TOKEN=' + secrets.token_urlsafe(48) + '\n')
text = text.replace('ENCRYPTION_KEY=\n', 'ENCRYPTION_KEY=' + Fernet.generate_key().decode() + '\n')
text = text.replace('WG_AGENT_TOKEN=\n', 'WG_AGENT_TOKEN=' + secrets.token_urlsafe(48) + '\n')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(text)
print('Created .env (0600). Configure PUBLIC_URL, providers and payment settings.')
