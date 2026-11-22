## WA Control & Live

Open a local terminal and show the Hermes bot's pairing QR:

```sh
hermes wa-control-live pair
```

Scan from **WhatsApp → Linked Devices → Link a Device** on the bot's phone.
The plugin will run with your Hermes gateway.

After linking the bot, show the QR for another WhatsApp account with
`hermes whatsapp-control pair personal`. Use the same `-p PROFILE` as your
install on both commands when using a named profile.
