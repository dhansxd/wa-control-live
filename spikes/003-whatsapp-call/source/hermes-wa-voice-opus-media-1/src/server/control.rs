use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::Path as FsPath;

use axum::extract::rejection::JsonRejection;
use serde_json::Value;
use tempfile::NamedTempFile;
use whatsapp_rust::features::{
    GroupCreateOptions, GroupDescription, GroupError, GroupMetadata, GroupParticipantOptions,
    GroupSubject, PreviousDescription,
};
use whatsapp_rust::wacore::iq::{
    devices::RemoveCompanionDeviceSpec,
    groups::{GroupCreateIq, GroupParticipatingIq},
    spec::IqSpec,
};
use whatsapp_rust::wacore_binary::{JidExt, NodeContent};

use super::*;

pub(crate) mod contacts;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    bot_number: String,
    op: String,
    #[serde(default)]
    args: Value,
}

fn invalid(message: &str) -> ApiError {
    error(StatusCode::BAD_REQUEST, message)
}

fn text<'a>(args: &'a Value, name: &str) -> Result<&'a str, ApiError> {
    args.get(name)
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| error(StatusCode::BAD_REQUEST, format!("expected {name}")))
}

fn group_id(args: &Value) -> Result<Jid, ApiError> {
    let jid = outbound::validate_chat(text(args, "chat")?)?;
    if jid.is_group() {
        Ok(jid)
    } else {
        Err(invalid("expected a WhatsApp group"))
    }
}

fn participants(args: &Value) -> Result<Vec<Jid>, ApiError> {
    args.get("participants")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("expected a participant list"))?
        .iter()
        .map(|value| {
            let jid = value
                .as_str()
                .ok_or_else(|| invalid("invalid WhatsApp participant"))
                .and_then(outbound::validate_chat)?;
            if jid.is_pn() || jid.is_lid() {
                Ok(jid)
            } else {
                Err(invalid("invalid WhatsApp participant"))
            }
        })
        .collect()
}

fn subject(value: &str) -> Result<GroupSubject, ApiError> {
    GroupSubject::new(value).map_err(|_| invalid("invalid group subject"))
}

fn summary(group: GroupMetadata) -> Value {
    let participants = group
        .participants
        .into_iter()
        .map(|member| {
            let admin = if member.is_super_admin() {
                Some("superadmin")
            } else if member.is_admin() {
                Some("admin")
            } else {
                None
            };
            json!({
                "id": member.jid.to_non_ad().to_string(),
                "phone": member.phone_number.map(|jid| jid.to_non_ad().to_string()),
                "lid": member.lid.map(|jid| jid.to_non_ad().to_string()),
                "admin": admin,
            })
        })
        .collect::<Vec<_>>();
    json!({
        "id": group.id.to_string(),
        "subject": group.subject.unwrap_or_default(),
        "description": group.description.unwrap_or_default(),
        "announce": group.is_announcement,
        "restrict": group.is_locked,
        "participants": participants,
    })
}

fn definitive_rejection(code: u16) -> bool {
    matches!(
        code,
        400 | 401 | 403 | 404 | 405 | 406 | 409 | 413 | 415 | 429
    )
}

fn rejected(code: u16) -> ApiError {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(json!({"error": "WhatsApp declined the group operation", "rejection_status": code})),
    )
}

fn iq_error(err: IqError) -> ApiError {
    match err {
        IqError::ServerError { code, .. } if definitive_rejection(code) => rejected(code),
        _ => error(
            StatusCode::BAD_GATEWAY,
            "WhatsApp group operation is unconfirmed",
        ),
    }
}

fn group_error(err: GroupError) -> ApiError {
    match err {
        GroupError::Iq(err) => iq_error(err),
        GroupError::DescriptionConflict => rejected(409),
        GroupError::InvalidRequest(_) => invalid("invalid group request"),
        _ => error(
            StatusCode::BAD_GATEWAY,
            "WhatsApp group operation is unconfirmed",
        ),
    }
}

fn receipts_unavailable() -> ApiError {
    error(
        StatusCode::SERVICE_UNAVAILABLE,
        "WhatsApp group receipts are unavailable",
    )
}

fn group_receipt(directory: &FsPath, id: &MessageId) -> Result<Value, ApiError> {
    match outbound::open_regular(&directory.join(id.as_str())) {
        Ok(file) => serde_json::from_reader(file).map_err(|_| receipts_unavailable()),
        Err(err) if err.kind() == io::ErrorKind::NotFound => {
            Ok(json!({"request_id": id.as_str(), "status": "unknown"}))
        }
        Err(_) => Err(receipts_unavailable()),
    }
}

fn mark_group_request(directory: &FsPath, id: &MessageId) -> io::Result<bool> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(directory)?;
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(directory.join(format!("{id}.pending")))
    {
        Ok(file) => {
            file.sync_all()?;
            File::open(directory)?.sync_all()?;
            Ok(true)
        }
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists => Ok(false),
        Err(err) => Err(err),
    }
}

async fn group_create(
    bridge: &Arc<Bridge>,
    bot_number: &str,
    args: &Value,
) -> Result<Value, ApiError> {
    let id = outbound::validate_id(text(args, "request_id")?)?;
    let subject = subject(text(args, "subject")?)?;
    let participants = participants(args)?;
    let directory = outbound::state_root()?
        .join("control-group-requests")
        .join(bot_number);
    if directory.join(format!("{id}.pending")).is_file() {
        return group_receipt(&directory, &id);
    }
    let client = require_owner_transport(bridge)?;
    let mut options = GroupCreateOptions::new(subject.into_string());
    for jid in participants {
        let mut participant = GroupParticipantOptions::new(jid.clone());
        if jid.is_lid() {
            let pn = client
                .get_lid_pn_entry(&jid)
                .await
                .map_err(|_| {
                    error(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "WhatsApp participant lookup unavailable",
                    )
                })?
                .filter(|entry| entry.lid.as_ref() == jid.user.as_str())
                .ok_or_else(|| {
                    invalid("no known phone number for the selected group participant")
                })?;
            participant = participant.with_phone_number(Jid::pn(&*pn.phone_number));
        }
        options = options.with_participant(participant);
    }

    let spec = GroupCreateIq::new(options);
    let mut request = spec.build_iq();
    request.id = Some(id.to_string());
    // Use the same operation identifier in the protocol's create key and in
    // the reply waiter, so timing out the HTTP caller cannot cause a new group.
    if let Some(NodeContent::Nodes(nodes)) = request.content.as_mut()
        && let Some(create) = nodes.first_mut()
    {
        create.attrs.insert("key", id.to_string());
    }

    if !mark_group_request(&directory, &id).map_err(|_| receipts_unavailable())? {
        return group_receipt(&directory, &id);
    }
    let bridge = bridge.clone();
    let unknown = json!({"request_id": id.as_str(), "status": "unknown"});
    let response_on_timeout = unknown.clone();
    let task = tokio::spawn(async move {
        let client = require_owner_transport(&bridge)?;
        let receipt = match client.send_iq(request).await {
            Ok(reply) => match spec.parse_response(reply.get()) {
                Ok(group) if group.id.is_group() => json!({
                    "request_id": id.as_str(), "status": "accepted", "group": summary(group.into()),
                }),
                _ => return Ok(unknown),
            },
            Err(IqError::ServerError { code, .. }) if definitive_rejection(code) => json!({
                "request_id": id.as_str(), "status": "rejected", "rejection_status": code,
            }),
            Err(_) => return Ok(unknown),
        };
        let mut temporary =
            NamedTempFile::new_in(&directory).map_err(|_| receipts_unavailable())?;
        serde_json::to_writer(temporary.as_file_mut(), &receipt)
            .map_err(|_| receipts_unavailable())?;
        temporary
            .flush()
            .and_then(|()| temporary.as_file().sync_all())
            .map_err(|_| receipts_unavailable())?;
        temporary
            .persist(directory.join(id.as_str()))
            .map_err(|_| receipts_unavailable())?;
        File::open(&directory)
            .and_then(|file| file.sync_all())
            .map_err(|_| receipts_unavailable())?;
        Ok(receipt)
    });
    match tokio::time::timeout(Duration::from_secs(10), task).await {
        Ok(Ok(result)) => result,
        Ok(Err(_)) => Err(receipts_unavailable()),
        Err(_) => Ok(response_on_timeout),
    }
}

async fn group_members(client: &Client, args: &Value) -> Result<Value, ApiError> {
    let chat = group_id(args)?;
    let participants = participants(args)?;
    if participants.is_empty() {
        return Err(invalid("expected at least one participant"));
    }
    let groups = client.groups();
    let result = match text(args, "action")? {
        "add" => groups.add_participants(chat, &participants).await,
        "remove" => groups.remove_participants(chat, &participants).await,
        "promote" => groups.promote_participants(chat, &participants).await,
        "demote" => groups.demote_participants(chat, &participants).await,
        _ => return Err(invalid("unsupported participant action")),
    }
    .map_err(group_error)?;

    let mut changes = Vec::with_capacity(result.len());
    for change in result {
        let jid = change.jid.to_non_ad();
        let phone = change.phone_number.as_ref().map(Jid::to_non_ad);
        let original = participants
            .iter()
            .find(|participant| **participant == jid || phone.as_ref() == Some(participant));
        let id = if let Some(original) = original {
            original.clone()
        } else if let Ok(Some(alias)) = client.get_lid_pn_entry(&jid).await {
            let lid = Jid::lid(&*alias.lid);
            let pn = Jid::pn(&*alias.phone_number);
            participants
                .iter()
                .find(|participant| {
                    (jid == lid && **participant == pn) || (jid == pn && **participant == lid)
                })
                .cloned()
                .unwrap_or(jid)
        } else {
            jid
        };
        let status = change.error.as_deref().unwrap_or("200");
        changes.push(json!({"id": id.to_string(), "status": status}));
    }
    Ok(json!(changes))
}

async fn group_settings(client: &Client, args: &Value) -> Result<Value, ApiError> {
    let chat = group_id(args)?;
    let subject = args
        .get("subject")
        .map(|_| text(args, "subject").and_then(subject))
        .transpose()?;
    let description = args
        .get("description")
        .map(|value| match value.as_str() {
            Some("") => Ok(None),
            Some(value) => GroupDescription::new(value)
                .map(Some)
                .map_err(|_| invalid("invalid group description")),
            None => Err(invalid("invalid group description")),
        })
        .transpose()?;
    let boolean = |key| {
        args.get(key)
            .map(|value| {
                value
                    .as_bool()
                    .ok_or_else(|| invalid("invalid group setting"))
            })
            .transpose()
    };
    let announce = boolean("announce")?;
    let restrict = boolean("restrict")?;
    if subject.is_none() && description.is_none() && announce.is_none() && restrict.is_none() {
        return Err(invalid("expected at least one group setting"));
    }
    let groups = client.groups();
    let mut updated = Vec::new();
    let result = async {
        if let Some(subject) = subject {
            groups.set_subject(chat.clone(), subject).await?;
            updated.push("subject");
        }
        if let Some(description) = description {
            groups
                .set_description(chat.clone(), description, PreviousDescription::Resolve)
                .await?;
            updated.push("description");
        }
        if let Some(announce) = announce {
            groups.set_announce(chat.clone(), announce).await?;
            updated.push("announce");
        }
        if let Some(restrict) = restrict {
            groups.set_locked(chat, restrict).await?;
            updated.push("restrict");
        }
        Ok::<_, GroupError>(())
    }
    .await;

    match result {
        Ok(()) => Ok(json!({"updated": updated})),
        Err(err) if updated.is_empty() => Err(group_error(err)),
        Err(_) => Ok(json!({"updated": updated, "incomplete": true})),
    }
}

/// Use the same recent-message types as the existing Control bridge. The Rust
/// convenience forward method unwraps view-once messages, so check the source
/// before calling the shared forward transformation.
pub(crate) fn forwardable(message: &wa::Message) -> Option<(&wa::Message, &'static str)> {
    if message.is_view_once() {
        return None;
    }
    let content = message.get_base_message();
    let kind = if content.conversation.is_some() || content.extended_text_message.is_set() {
        "text"
    } else if content.image_message.is_set() {
        "image"
    } else if content.video_message.is_set() {
        "video"
    } else if content.audio_message.is_set() {
        "audio"
    } else if content.document_message.is_set() {
        "document"
    } else {
        return None;
    };
    Some((content, kind))
}

fn recent_messages(bridge: &Bridge, args: &Value) -> Result<Value, ApiError> {
    let chat = outbound::validate_chat(text(args, "chat")?)?;
    let id = args
        .get("message_id")
        .map(|_| text(args, "message_id"))
        .transpose()?;
    let limit = args
        .get("limit")
        .map(|value| {
            value
                .as_u64()
                .filter(|n| (1..=30).contains(n))
                .ok_or_else(|| invalid("message list limit must be between 1 and 30"))
        })
        .transpose()?
        .unwrap_or(10) as usize;
    let inbox = bridge.control_inbox.as_ref().ok_or_else(|| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "WhatsApp message cache is unavailable",
        )
    })?;
    let messages = if let Some(id) = id {
        inbox.find(&chat, id).into_iter().collect()
    } else {
        inbox.recent(&chat)
    };
    let results = messages.iter().filter_map(|record| {
        let (content, kind) = forwardable(&record.message)?;
        Some(json!({
            "id": record.info.id.as_str(),
            "type": kind,
            "timestamp": record.info.timestamp.timestamp(),
            "text": content.text_content().or_else(|| content.get_caption()).unwrap_or_default(),
            "from_me": record.info.source.is_from_me,
        }))
    }).take(limit).collect::<Vec<_>>();
    Ok(json!(results))
}

pub(super) async fn execute(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    body: Result<Json<Request>, JsonRejection>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    let Json(Request {
        bot_number,
        op,
        args,
    }) = body.map_err(|_| invalid("invalid control request"))?;
    outbound::check_configured_bot(&bridge, &bot_number)?;
    if op == "group_create_result" {
        let directory = outbound::state_root()?
            .join("control-group-requests")
            .join(&bot_number);
        let id = outbound::validate_id(text(&args, "request_id")?)?;
        return Ok(Json(group_receipt(&directory, &id)?).into_response());
    }

    let client = require_owner_transport(&bridge)?;
    let result = match op.as_str() {
        "contacts" => contacts::list(
            client,
            &bot_number,
            args.get("saved_only") == Some(&Value::Bool(true)),
        )?,
        "group_list" => {
            let response = client
                .execute(GroupParticipatingIq::new())
                .await
                .map_err(iq_error)?;
            json!(
                response
                    .groups
                    .into_iter()
                    .map(|group| summary(group.into()))
                    .collect::<Vec<_>>()
            )
        }
        "group_metadata" => summary(
            client
                .groups()
                .fetch_metadata(&group_id(&args)?)
                .await
                .map_err(group_error)?,
        ),
        "group_create" => group_create(&bridge, &bot_number, &args).await?,
        "group_members" => group_members(client, &args).await?,
        "group_settings" => group_settings(client, &args).await?,
        "recent_messages" => recent_messages(&bridge, &args)?,
        "forward_message" => {
            let chat = outbound::validate_chat(text(&args, "chat")?)?;
            let source = outbound::validate_chat(text(&args, "source_chat")?)?;
            let id = outbound::validate_id(text(&args, "message_id")?)?;
            return outbound::forward_message(
                &bridge,
                &bot_number,
                chat,
                id,
                &source,
                text(&args, "source_id")?,
            )
            .await;
        }
        "logout" => {
            let own = client.pn().ok_or_else(|| {
                error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "WhatsApp device identity unavailable",
                )
            })?;
            let confirmed = client
                .execute(RemoveCompanionDeviceSpec::new(&own))
                .await
                .is_ok();
            json!({"unlink_requested": true, "remote_unlink_confirmed": confirmed})
        }
        _ => return Err(invalid("unsupported WhatsApp control operation")),
    };
    Ok(Json(result).into_response())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn group_request_is_claimed_once_and_does_not_infer_success() {
        let directory = tempfile::tempdir().unwrap();
        let id = outbound::validate_id("3EB0AABBCCDD").unwrap();
        assert!(mark_group_request(directory.path(), &id).unwrap());
        assert!(!mark_group_request(directory.path(), &id).unwrap());
        assert_eq!(
            group_receipt(directory.path(), &id).unwrap(),
            json!({"request_id": "3EB0AABBCCDD", "status": "unknown"})
        );
    }

    #[test]
    fn forwards_text_and_preserves_view_once_exclusion() {
        let text = wa::Message::text("hello");
        assert_eq!(forwardable(&text).unwrap().1, "text");
        let mut source = wa::Message::default();
        let mut image = wa::message::ImageMessage::default();
        image.view_once = Some(true);
        source.image_message = MessageField::some(image);
        assert!(forwardable(&source).is_none());
    }
}
