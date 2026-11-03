# Security

## Reporting a problem

Please don't describe a security problem in a public issue. Use **Report a vulnerability** on this
repository's Security tab if it's available. If it isn't, open an issue asking for a private way to
get in touch, without any details of the problem.

## How jetbridge handles your details

- Your Gmail app password is kept in Windows Credential Manager or Mac Keychain, never in a file or a
  log.
- jetbridge never asks for the password in a chat. Claude opens a private window on your PC for it.
- Your watches, addresses and settings stay on your PC, in `%LOCALAPPDATA%\jetbridge`.
- It never logs in to an airline account, books anything or moves your miles.
- If you use it through Claude, the details handled in that chat go to Claude like the rest of the
  conversation.

If an app password is ever exposed, revoke it in
[your Google account](https://myaccount.google.com/apppasswords) and run `jetbridge notify setup`.
