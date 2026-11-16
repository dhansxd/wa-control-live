use std::path::PathBuf;
use std::sync::Weak;

use serde::Serialize;
use tokio::sync::mpsc;
use whatsapp_rust::WAPatchName;

use super::*;

#[derive(Deserialize, Serialize)]
struct Contact {
    id: String,
    name: String,
    phone: String,
    lid: String,
    saved: bool,
}

impl Contact {
    fn matches(&self, jid: &str) -> bool {
        self.id == jid || self.phone == jid || self.lid == jid
    }
}

fn known_address(jid: &Jid) -> Option<Jid> {
    let jid = jid.to_non_ad();
    if (jid.is_pn() || jid.is_lid())
        && !jid.user.is_empty()
        && jid.user.bytes().all(|byte| byte.is_ascii_digit())
    {
        Some(jid)
    } else {
        None
    }
}

fn directory(bot: &str) -> Result<PathBuf, ApiError> {
    Ok(outbound::state_root()?.join("control-contacts").join(bot))
}

fn read(directory: &FsPath) -> io::Result<Vec<(PathBuf, Contact)>> {
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(err) => return Err(err),
    };
    entries
        .filter_map(|entry| match entry {
            Ok(entry)
                if entry
                    .path()
                    .extension()
                    .is_some_and(|extension| extension == "json") =>
            {
                let path = entry.path();
                Some(
                    outbound::open_regular(&path)
                        .and_then(|file| Ok((path, serde_json::from_reader(file)?))),
                )
            }
            Ok(_) => None,
            Err(err) => Some(Err(err)),
        })
        .collect()
}

fn apply_contact_event(directory: &FsPath, event: &Event) -> io::Result<()> {
    match event {
        Event::ContactUpdate(change) => {
            let Some(jid) = known_address(&change.jid) else {
                return Ok(());
            };
            // ContactAction is the address-book mutation. Chat push names and
            // history are deliberately not used to broaden a wildcard send.
            let Some(name) = change
                .action
                .full_name
                .as_deref()
                .filter(|name| !name.trim().is_empty())
                .or_else(|| {
                    change
                        .action
                        .first_name
                        .as_deref()
                        .filter(|name| !name.trim().is_empty())
                })
            else {
                return Ok(());
            };
            let alias = |value: Option<&str>| {
                value
                    .and_then(|value| value.parse::<Jid>().ok())
                    .as_ref()
                    .and_then(known_address)
            };
            let phone = if jid.is_pn() {
                Some(jid.clone())
            } else {
                alias(change.action.pn_jid.as_deref()).filter(Jid::is_pn)
            };
            let lid = if jid.is_lid() {
                Some(jid.clone())
            } else {
                alias(change.action.lid_jid.as_deref()).filter(Jid::is_lid)
            };
            let contact = Contact {
                id: jid.to_string(),
                name: name.to_string(),
                phone: phone.map(|jid| jid.to_string()).unwrap_or_default(),
                lid: lid.map(|jid| jid.to_string()).unwrap_or_default(),
                saved: true,
            };
            DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(directory)?;
            let mut temporary = NamedTempFile::new_in(directory)?;
            serde_json::to_writer(temporary.as_file_mut(), &contact)?;
            temporary.flush()?;
            temporary.persist(directory.join(format!("{}.json", contact.id)))?;
        }
        Event::ContactRemoved(change) => {
            let Some(jid) = known_address(&change.jid) else {
                return Ok(());
            };
            let deleted = jid.to_string();
            for (path, contact) in read(directory)? {
                if contact.matches(&deleted) {
                    fs::remove_file(path)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

struct Observer {
    client: Weak<Client>,
    bot: String,
    pending: mpsc::UnboundedSender<Option<Arc<Event>>>,
}

impl EventHandler for Observer {
    fn handle_event(&self, event: Arc<Event>) {
        if self
            .client
            .upgrade()
            .is_some_and(|client| owner_chat::selected_bot(&client, &self.bot))
        {
            let _ = self.pending.send(Some(event));
        }
    }
}

pub(crate) fn subscribe(
    client: &Arc<Client>,
    expected_bot: Option<&str>,
    recover: bool,
) -> Option<(Subscription, tokio::task::JoinHandle<()>)> {
    let bot = expected_bot?;
    let directory = directory(bot).ok()?;
    let (pending, mut events) = mpsc::unbounded_channel();
    let subscription = client.subscribe(
        EventInterest::of(&[EventKind::ContactUpdate, EventKind::ContactRemoved]),
        Arc::new(Observer {
            client: Arc::downgrade(client),
            bot: bot.to_string(),
            pending: pending.clone(),
        }),
    );

    // A previously paired device may already have advanced past the
    // address-book sync version. Rebuild this new projection once if missing.
    let synchronized = directory.join(".synced");
    if recover && !synchronized.is_file() {
        let client = Arc::downgrade(client);
        let bot = bot.to_string();
        tokio::spawn(async move {
            let Some(client) = client.upgrade() else {
                return;
            };
            if tokio::time::timeout(Duration::from_secs(30), async {
                while !client.is_logged_in() {
                    tokio::time::sleep(Duration::from_millis(250)).await;
                }
            })
            .await
            .is_ok()
                && owner_chat::selected_bot(&client, &bot)
                && client
                    .resync_app_state_collection(WAPatchName::CriticalUnblockLow)
                    .await
                    .is_ok_and(|report| report.all_synced())
            {
                let _ = pending.send(None);
            }
        });
    }

    let worker = tokio::spawn(async move {
        let mut write_failed = false;
        while let Some(event) = events.recv().await {
            let directory = directory.clone();
            let result = tokio::task::spawn_blocking(move || {
                if let Some(event) = event {
                    apply_contact_event(&directory, &event)
                } else if !write_failed {
                    DirBuilder::new()
                        .recursive(true)
                        .mode(0o700)
                        .create(&directory)?;
                    OpenOptions::new()
                        .create(true)
                        .write(true)
                        .truncate(true)
                        .mode(0o600)
                        .open(directory.join(".synced"))?;
                    Ok(())
                } else {
                    Ok(())
                }
            })
            .await;
            if !matches!(result, Ok(Ok(()))) {
                write_failed = true;
                let _ = fs::remove_file(&synchronized);
                log::warn!("could not update WhatsApp contacts");
            }
        }
    });
    Some((subscription, worker))
}

pub(super) fn list(client: &Client, bot: &str, saved_only: bool) -> Result<Value, ApiError> {
    let own = [client.pn(), client.lid()]
        .into_iter()
        .flatten()
        .map(|jid| jid.to_non_ad().to_string())
        .collect::<Vec<_>>();
    let directory = directory(bot)?;
    if saved_only && !directory.join(".synced").is_file() {
        return Err(error(
            StatusCode::SERVICE_UNAVAILABLE,
            "WhatsApp address-book sync is still unavailable",
        ));
    }
    let mut contacts = read(&directory)
        .map_err(|_| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "WhatsApp contacts are unavailable",
            )
        })?
        .into_iter()
        .map(|(_, contact)| contact)
        .filter(|contact| !own.iter().any(|jid| contact.matches(jid)))
        .collect::<Vec<_>>();
    contacts.sort_by(|a, b| a.id.cmp(&b.id));
    Ok(json!(contacts))
}

#[cfg(test)]
mod tests {
    use super::*;
    use whatsapp_rust::wacore::types::events::{ContactRemoved, ContactUpdate};

    #[test]
    fn contact_projection_keeps_address_book_removals_and_aliases() {
        let directory = tempfile::tempdir().unwrap();
        let id = "551199112233@lid".parse().unwrap();
        let mut action = wa::sync_action_value::ContactAction::default();
        action.full_name = Some("Saved name".into());
        action.pn_jid = Some("551188112233@s.whatsapp.net".into());
        let contact = ContactUpdate::builder()
            .jid(id)
            .timestamp(Default::default())
            .from_full_sync(true)
            .action(Box::new(action))
            .build();
        apply_contact_event(directory.path(), &Event::ContactUpdate(contact)).unwrap();
        let records = read(directory.path()).unwrap();
        assert_eq!(records.len(), 1);
        assert!(records[0].1.matches("551188112233@s.whatsapp.net"));
        let remove = ContactRemoved::builder()
            .jid("551188112233@s.whatsapp.net".parse().unwrap())
            .timestamp(Default::default())
            .from_full_sync(false)
            .build();
        apply_contact_event(directory.path(), &Event::ContactRemoved(remove)).unwrap();
        assert!(read(directory.path()).unwrap().is_empty());
    }
}
