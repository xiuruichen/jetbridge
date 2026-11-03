# Trying jetbridge on a Mac

jetbridge on a Mac is experimental. Its automated tests run on GitHub's Mac machines, but nobody has
tried it on a real Mac yet, so these are the things only a real Mac can answer. It should take about
20 minutes, plus a night for the sleep check.

Please [report what you find](https://github.com/xiuruichen/jetbridge/issues/new?template=mac_report.yml),
even if everything works. Please don't include your email address or anything else personal.

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/xiuruichen/jetbridge/main/install.sh | bash
```

- Did the install finish, and does `jetbridge --version` work in a new Terminal window?
- Did `jetbridge doctor` show anything marked `FAIL`?

## 2. A real search

Add a flight with `jetbridge start`, or run `jetbridge check 1` for one you've added.

- **Did you get a clear answer**, or `unknown`? If it's `unknown`, what did it say? `blocked` is
  especially useful to know, because airline sites might treat a Mac differently.
- **Did Chrome appear on your screen or take focus during the check?** This is the biggest unknown,
  because macOS may not let a window stay off-screen.

## 3. Automatic checks

Leave your Mac awake for an hour, then run `jetbridge status`.

- Did `Last tick` keep updating?
- Did macOS ask for any permission, or show a notice about a background item?

## 4. Email

- Did macOS ask to let jetbridge use your Keychain, and did that keep happening during scheduled
  checks? Did the test email arrive?

## 5. Sleep

Run `jetbridge keep-awake on`, leave your Mac plugged in overnight with the lid open, and check
`jetbridge status` in the morning. Then run `jetbridge keep-awake off`.

- Did checks run overnight?
- Without keep-awake, did one check run soon after your Mac woke up?

## 6. Claude Desktop

Run `jetbridge claude connect`, quit Claude Desktop with Cmd+Q, and reopen it.

- Do the tools appear? Does "Show me my flights" draw the board?
- Does "Set up email" open a Terminal window, or did macOS refuse?
