//! hermes-wa-voice: WhatsApp voice-call sidecar for Hermes.
//!
//!   hermes-wa-voice pair [--phone <number>]   Link to the bot's WhatsApp number (QR or pair code)
//!   hermes-wa-voice serve                      Answer the owner's calls and place calls on request
//!
//! Normally started through `hermes whatsapp-voice ...`, which supplies the environment.

mod audio;
mod calls;
mod config;
mod control_inbox;
mod core;
mod gemini;
mod hermes;
mod media_diagnostics;
mod owner_chat;
mod relay;
mod server;

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use qrcode::QrCode;
use qrcode::render::unicode::Dense1x2;
use whatsapp_rust::pair_code::PairCodeOptions;
use whatsapp_rust::prelude::*;
use whatsapp_rust::wacore::types::call::CallAction;

use crate::calls::{Bridge, Signal};
use crate::config::{Config, digits};

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("info,whatsapp_rust=warn,wacore=warn"),
    )
    .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("pair") => {
            let phone = match args.get(1).map(String::as_str) {
                Some("--phone") => Some(digits(args.get(2).context("--phone needs a number")?)),
                Some(other) => bail!("unknown argument {other}"),
                None => None,
            };
            pair(phone).await
        }
        Some("serve") => serve().await,
        Some("--version") => {
            println!("{}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        _ => bail!("usage: hermes-wa-voice pair [--phone <number>] | serve"),
    }
}

async fn open_store() -> Result<SqliteStore> {
    let dir = Config::data_dir()?;
    std::fs::create_dir_all(&dir)?;
    SqliteStore::open(&Config::store_path(&dir))
        .await
        .map_err(|e| anyhow!("opening session store: {e}"))
}

async fn pair_confirmation(done: &mut tokio::sync::mpsc::Receiver<()>) -> Result<()> {
    done.recv()
        .await
        .context("WhatsApp client stopped before confirming the linked device")
}

async fn pair(phone: Option<String>) -> Result<()> {
    let expected_bot = Config::expected_bot()?;
    let (done_tx, mut done_rx) = tokio::sync::mpsc::channel::<()>(1);
    // The dashboard's Pair button shows this file, refreshed on every QR rotation.
    let qr_file = Config::data_dir()?.join("pair-qr.svg");
    let qr_path = qr_file.clone();
    let mut builder = Bot::builder()
        .with_backend(open_store().await?)
        .on_qr_code(move |code, timeout| {
            let qr_path = qr_path.clone();
            async move {
                match QrCode::new(code.as_bytes()) {
                    Ok(qr) => {
                        println!("\n{}", qr.render::<Dense1x2>().quiet_zone(true).build());
                        let svg = qr.render::<qrcode::render::svg::Color>().min_dimensions(320, 320).build();
                        let tmp = qr_path.with_extension("svg.tmp");
                        if std::fs::write(&tmp, svg).and_then(|_| std::fs::rename(&tmp, &qr_path)).is_err() {
                            log::warn!("could not write {}", qr_path.display());
                        }
                    }
                    Err(_) => println!("\n{code}"),
                }
            println!(
                "On the bot's phone: WhatsApp > Linked devices > Link a device, then scan (valid {}s).",
                    timeout.as_secs()
                );
            }
        })
        .on_pair_code(|code, timeout| async move {
            println!("\nPair code: {code}  (valid {}s)", timeout.as_secs());
            println!("On the bot's phone: WhatsApp > Linked devices > Link a device > Link with phone number instead.");
        })
        .on_connected(move |_client| {
            let done = done_tx.clone();
            async move {
                let _ = done.send(()).await;
            }
        });
    if let Some(phone_number) = phone {
        builder = builder.with_pair_code(PairCodeOptions {
            phone_number,
            ..Default::default()
        });
    }
    let bot = builder
        .build()
        .await
        .map_err(|e| anyhow!("starting WhatsApp client: {e}"))?;
    let contacts = server::subscribe_control_contacts(&bot.client(), expected_bot.as_deref(), false);
    let mut bot = bot.spawn();
    tokio::select! {
        reason = &mut bot => bail!("WhatsApp client stopped before linking: {reason:?}"),
        confirmation = pair_confirmation(&mut done_rx) => confirmation?,
    }
    if expected_bot
        .as_deref()
        .is_some_and(|expected| !owner_chat::selected_bot(&bot.client(), expected))
    {
        bot.shutdown().await;
        let _ = std::fs::remove_file(&qr_file);
        bail!("Linked identity does not match HWV_BOT_NUMBER");
    }
    // Let post-login setup finish, then shut down cleanly: only shutdown() saves the new session.
    tokio::time::sleep(Duration::from_secs(5)).await;
    bot.shutdown().await;
    if let Some((observer, worker)) = contacts {
        drop(observer);
        let _ = worker.await;
    }
    let _ = std::fs::remove_file(&qr_file);
    println!("Linked. Start the sidecar with: hermes whatsapp-voice serve");
    Ok(())
}

struct CallObserver {
    bridge: Arc<Bridge>,
}

impl EventHandler for CallObserver {
    fn handle_event(&self, event: Arc<Event>) {
        let Event::IncomingCall(call) = &*event else {
            return;
        };
        if self.bridge.cfg.expected_bot.is_some() && !owner_chat::account_ready(&self.bridge) {
            return;
        }
        let (call_creator, caller_pn) = match &call.action {
            CallAction::Offer {
                call_creator,
                caller_pn,
                group_jid: None,
                ..
            } => (call_creator, caller_pn),
            CallAction::PreAccept { call_id, .. } => {
                return self.bridge.ring_signals.send(call_id, Signal::Ringing);
            }
            CallAction::Accept { call_id, .. } => {
                return self.bridge.ring_signals.send(call_id, Signal::Accepted);
            }
            // `busy`/`enc` mean one device could not take the call; the others keep ringing.
            CallAction::Reject {
                call_id,
                reason: None,
                ..
            } => return self.bridge.ring_signals.send(call_id, Signal::Declined),
            _ => return,
        };
        if !self.bridge.cfg.answer_inbound {
            return;
        }
        let (bridge, call) = (self.bridge.clone(), (**call).clone());
        let (creator, caller_pn) = (call_creator.clone(), caller_pn.clone());
        tokio::spawn(async move {
            // Anyone but the owner is left alone: the bot's other devices can still pick up.
            if !bridge.is_owner(&call, &creator, caller_pn.as_ref()).await {
                log::info!("ignoring call from {creator}: not the owner");
                return;
            }
            bridge.run_inbound(call).await;
        });
    }
}

async fn serve() -> Result<()> {
    let cfg = Config::from_env()?;
    let bot = Bot::builder()
        .with_backend(open_store().await?)
        .on_qr_code(|_, _| async {
            log::error!("not linked to WhatsApp yet; run: hermes whatsapp-voice pair");
            // Exit 0 so service managers (Restart=on-failure) don't retry until someone re-pairs.
            std::process::exit(0);
        })
        .on_connected(|_| async { log::info!("connected to WhatsApp") })
        .on_logged_out(|info| async move {
            log::error!("logged out of WhatsApp ({info:?}); run: hermes whatsapp-voice pair");
            std::process::exit(0);
        })
        .build()
        .await
        .map_err(|e| anyhow!("starting WhatsApp client: {e}"))?;
    let client = bot.client();
    client.set_relay_transport_provider(Arc::new(relay::NativeRelayFallback));
    let bridge = Arc::new(Bridge::new(cfg, Some(client.clone())));
    let _subscription = client.subscribe(
        EventInterest::of(&[EventKind::IncomingCall]),
        Arc::new(CallObserver {
            bridge: bridge.clone(),
        }),
    );
    let contacts = server::subscribe_control_contacts(
        &client,
        bridge.cfg.expected_bot.as_deref(),
        true,
    );
    let _control_messages = bridge.control_inbox.as_ref().map(|_| {
        client.subscribe(
            EventInterest::of(&[EventKind::Messages]),
            Arc::new(control_inbox::ControlObserver(bridge.clone())),
        )
    });
    let owner_messages = bridge.owner_inbox.as_ref().map(|_| {
        let (pending, messages) = tokio::sync::mpsc::channel(256);
        let observer = client.subscribe(
            EventInterest::of(&[EventKind::Messages]),
            Arc::new(owner_chat::OwnerMessageObserver {
                bridge: bridge.clone(),
                pending,
            }),
        );
        let worker = tokio::spawn(owner_chat::retain_owner_messages(bridge.clone(), messages));
        (observer, worker)
    });
    let mut bot = bot.spawn();
    if bridge.cfg.hermes_api_key.is_empty() {
        log::warn!("API_SERVER_KEY not set: calls work, but the voice agent cannot reach Hermes");
    }
    tokio::spawn({
        let bridge = bridge.clone();
        async move { bridge.refresh_persona().await }
    });

    let res = tokio::select! {
        reason = &mut bot => bail!("WhatsApp client stopped: {reason:?}"),
        res = server::serve(bridge) => res,
        _ = shutdown_signal() => Ok(()),
    };
    if let Some((observer, worker)) = contacts {
        drop(observer);
        worker.abort();
        let _ = worker.await;
    }
    if let Some((observer, worker)) = owner_messages {
        drop(observer);
        worker.abort();
        let _ = worker.await;
    }
    // Saves the device session and encryption state; exiting without it can lose them.
    bot.shutdown().await;
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn pair_confirmation_does_not_accept_closed_channel() {
        let (done, mut confirmation) = tokio::sync::mpsc::channel::<()>(1);
        drop(done);
        assert!(pair_confirmation(&mut confirmation).await.is_err());
    }

    #[tokio::test]
    async fn pair_confirmation_accepts_connected_event() {
        let (done, mut confirmation) = tokio::sync::mpsc::channel::<()>(1);
        done.send(()).await.unwrap();
        drop(done);
        assert!(pair_confirmation(&mut confirmation).await.is_ok());
    }
}
