Enable `hermes-whatsapp-control` in your Hermes profile. In a local terminal, run `hermes whatsapp-control setup --owner OWNER_NUMBER --command-account HERMES_NUMBER`. Use international numbers. The owner sends commands to the Hermes number. Omit `--command-account` if both use the same number.

Run `hermes whatsapp-control serve` and leave it running. In a second terminal, run `hermes whatsapp-control pair shop` with a lowercase label of your choice. Scan the QR from that account's **Linked Devices** screen, then run `hermes whatsapp-control status`. Repeat `pair` for additional accounts. Use the same Hermes profile in both terminals.

Natural-language owner commands require Hermes 0.23.1 or later and a verified owner DM. See the README for dependency setup, chatbot settings, and how to retain or purge sessions before removing the plugin.
