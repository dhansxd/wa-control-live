use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path as FsPath, PathBuf};

use axum::extract::rejection::JsonRejection;
use serde::Serialize;
use tempfile::NamedTempFile;
use whatsapp_rust::download::MediaType;
use whatsapp_rust::media::{self, AudioOptions, DocumentOptions, ImageOptions, VideoOptions};
use whatsapp_rust::wacore::types::LearningSource;
use whatsapp_rust::wacore::upload::{EncryptedMediaInfo, UploadSource, encrypt_media_streaming};
use whatsapp_rust::wacore_binary::JidExt;
use whatsapp_rust::{Freshness, StatusPrivacySetting, StatusSendOptions};

use super::*;
use crate::calls::NoticeDm;
use crate::config::Config;

const STATUS_BACKGROUND: u32 = 0xFF303030;
const STATUS_VOICE_MIME: &str = "audio/ogg; codecs=opus";

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Kind {
    Text,
    Image,
    Document,
    Video,
    Audio,
    Voice,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct MessageRequest {
    bot_number: String,
    chat_id: String,
    message_id: String,
    kind: Kind,
    text: Option<String>,
    path: Option<PathBuf>,
    expected_sha256: Option<String>,
    mimetype: Option<String>,
    filename: Option<String>,
    audience: Option<Vec<String>>,
}

#[derive(Deserialize)]
pub(super) struct ReceiptQuery {
    bot_number: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum Outcome {
    Accepted,
    Rejected,
    Unknown,
}

#[derive(Debug, Deserialize, PartialEq, Serialize)]
struct Receipt {
    message_id: String,
    chat_id: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    audience: Vec<String>,
    status: Outcome,
    #[serde(skip_serializing_if = "Option::is_none")]
    rejection_status: Option<u32>,
}

pub(super) fn state_root() -> Result<PathBuf, ApiError> {
    Config::data_dir()
        .ok()
        .and_then(|path| path.parent()?.canonicalize().ok())
        .ok_or_else(|| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "WhatsApp runtime state is unavailable",
            )
        })
}

pub(super) fn check_configured_bot(bridge: &Bridge, bot_number: &str) -> Result<(), ApiError> {
    match bridge.cfg.expected_bot.as_deref() {
        Some(expected) if expected == bot_number => Ok(()),
        Some(_) => Err(error(
            StatusCode::FORBIDDEN,
            "selected WhatsApp bot does not match",
        )),
        None => Err(error(
            StatusCode::NOT_FOUND,
            "WhatsApp Control transport is disabled",
        )),
    }
}

pub(super) fn validate_id(id: &str) -> Result<MessageId, ApiError> {
    if !(8..=80).contains(&id.len())
        || !id
            .bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == b'-' || ch == b'_')
    {
        return Err(error(StatusCode::BAD_REQUEST, "invalid message ID"));
    }
    id.parse()
        .map_err(|_| error(StatusCode::BAD_REQUEST, "invalid message ID"))
}

pub(super) fn validate_chat(chat: &str) -> Result<Jid, ApiError> {
    chat.parse::<Jid>()
        .ok()
        .filter(|jid| {
            (jid.is_pn() || jid.is_lid() || jid.is_group())
                && !jid.user.is_empty()
                && jid.to_non_ad().to_string() == chat
        })
        .ok_or_else(|| error(StatusCode::BAD_REQUEST, "invalid WhatsApp destination"))
}

fn validate_audience(audience: Option<&[String]>) -> Result<Vec<Jid>, ApiError> {
    let audience = audience.filter(|users| !users.is_empty()).ok_or_else(|| {
        error(
            StatusCode::BAD_REQUEST,
            "select at least one Status recipient",
        )
    })?;
    let mut recipients = Vec::with_capacity(audience.len());
    for user in audience {
        let jid = validate_chat(user)?;
        if !is_status_user(&jid) {
            return Err(error(StatusCode::BAD_REQUEST, "invalid Status recipient"));
        }
        recipients.push(jid);
    }
    recipients.sort_by_cached_key(ToString::to_string);
    recipients.dedup();
    Ok(recipients)
}

fn is_status_user(jid: &Jid) -> bool {
    (jid.is_pn() || jid.is_lid())
        && !jid.user.is_empty()
        && jid.user != "0"
        && jid.integrator == 0
        && jid.user.bytes().all(|byte| byte.is_ascii_digit())
}

async fn verified_lid(client: &Client, jid: &Jid) -> Result<Option<Jid>, ApiError> {
    if jid.is_lid() {
        return Ok(Some(jid.clone()));
    }
    let entry = client.get_lid_pn_entry(jid).await.map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "Status audience lookup unavailable",
        )
    })?;
    Ok(entry
        .filter(|entry| {
            entry.phone_number.as_ref() == jid.user.as_str()
                && matches!(
                    entry.learning_source,
                    LearningSource::Usync
                        | LearningSource::RecipientLatestLid
                        | LearningSource::PeerPnMessage
                        | LearningSource::PeerLidMessage
                        | LearningSource::DeviceNotification
                        | LearningSource::Pairing
                )
        })
        .map(|entry| Jid::lid(entry.lid.as_ref()))
        .filter(is_status_user))
}

async fn status_recipients(client: &Client, audience: Vec<Jid>) -> Result<Vec<Jid>, ApiError> {
    let mut recipients = Vec::with_capacity(audience.len());
    let mut unresolved = Vec::new();
    for jid in audience {
        if let Some(lid) = verified_lid(client, &jid).await? {
            recipients.push(lid);
        } else {
            unresolved.push(jid);
        }
    }
    if !unresolved.is_empty() {
        let checked = client
            .contacts()
            .is_on_whatsapp(&unresolved)
            .await
            .map_err(|_| {
                error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Status audience lookup unavailable",
                )
            })?;
        for jid in unresolved {
            let lid = checked
                .iter()
                .find(|person| {
                    person.jid == jid
                        && person.is_registered
                        && person.contact_error.is_none()
                        && person.lid_error.is_none()
                })
                .and_then(|person| person.lid.as_ref())
                .filter(|lid| lid.is_lid() && is_status_user(lid))
                .ok_or_else(|| {
                    error(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "could not verify every Status recipient",
                    )
                })?;
            recipients.push(lid.to_non_ad());
        }
    }
    // Freeze the reviewed audience as bare LIDs. The pinned Status sender
    // otherwise resolves PNs again and may silently skip a missing mapping.
    recipients.sort_by_cached_key(ToString::to_string);
    recipients.dedup();
    Ok(recipients)
}

fn receipt_directory(root: &FsPath, bot: &str) -> PathBuf {
    root.join("outbound-receipts").join(bot)
}

pub(super) fn open_regular(path: &FsPath) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "expected a regular file",
        ));
    }
    Ok(file)
}

fn load_receipt(directory: &FsPath, id: &MessageId) -> io::Result<Option<Receipt>> {
    match open_regular(&directory.join(id.as_str())) {
        Ok(file) => {
            let receipt: Receipt = serde_json::from_reader(file)?;
            if receipt.message_id != id.as_str() || receipt.status == Outcome::Unknown {
                return Err(io::ErrorKind::InvalidData.into());
            }
            Ok(Some(receipt))
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(err),
    }
}

fn persist_receipt(directory: &FsPath, id: &MessageId, receipt: &Receipt) -> io::Result<()> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(directory)?;
    let mut temporary = NamedTempFile::new_in(directory)?;
    serde_json::to_writer(temporary.as_file_mut(), receipt)?;
    temporary.flush()?;
    temporary.as_file().sync_all()?;
    match temporary.persist_noclobber(directory.join(id.as_str())) {
        Ok(_) => (),
        Err(err)
            if err.error.kind() == io::ErrorKind::AlreadyExists
                && load_receipt(directory, id)?.as_ref() == Some(receipt) =>
        {
            ()
        }
        Err(err) => return Err(err.error),
    }
    File::open(directory)?.sync_all()?;
    Ok(())
}

struct MediaFile {
    file: NamedTempFile,
    length: u64,
}

impl UploadSource for MediaFile {
    fn len(&self) -> u64 {
        self.length
    }

    fn reader_from(&self, offset: u64) -> io::Result<Box<dyn Read + Send>> {
        let mut reader = self.file.reopen()?;
        reader.seek(SeekFrom::Start(offset))?;
        Ok(Box::new(reader))
    }
}

fn spool_attachment(
    root: &FsPath,
    path: &FsPath,
    expected: &str,
    media: MediaType,
    voice_note: bool,
) -> Result<(MediaFile, EncryptedMediaInfo), ApiError> {
    let spool = root.join("outbound");
    let valid_directory = fs::symlink_metadata(&spool).is_ok_and(|meta| meta.file_type().is_dir());
    let filename = path.file_name().filter(|_| {
        path.is_absolute()
            && valid_directory
            && path
                .parent()
                .and_then(|parent| parent.canonicalize().ok())
                .as_ref()
                == Some(&spool)
    });
    let filename = filename.ok_or_else(|| {
        error(
            StatusCode::BAD_REQUEST,
            "expected a file in the outbound spool",
        )
    })?;
    let mut source = open_regular(&spool.join(filename)).map_err(|_| {
        error(
            StatusCode::BAD_REQUEST,
            "expected a regular outbound spool file",
        )
    })?;
    let source: Box<dyn Read> = if voice_note {
        let mut header = [0; 47];
        if source.read_exact(&mut header).is_err() || !is_mono_opus_head(&header) {
            return Err(error(
                StatusCode::BAD_REQUEST,
                "Voice notes require mono Ogg Opus",
            ));
        }
        // Encrypt the exact bytes inspected above, even if the source changes.
        Box::new(io::Cursor::new(header).chain(source))
    } else {
        Box::new(source)
    };
    let mut file = NamedTempFile::new_in(&spool).map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "media spool is unavailable",
        )
    })?;
    let info = encrypt_media_streaming(source, file.as_file_mut(), media).map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "could not prepare attachment",
        )
    })?;
    let hash = info
        .file_sha256
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if info.file_length == 0 || hash != expected {
        return Err(error(
            StatusCode::BAD_REQUEST,
            "attachment is empty or changed after selection",
        ));
    }
    let length = file
        .as_file()
        .metadata()
        .map_err(|_| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "media spool is unavailable",
            )
        })?
        .len();
    Ok((MediaFile { file, length }, info))
}

fn is_mono_opus_head(header: &[u8; 47]) -> bool {
    &header[..4] == b"OggS"
        && header[4] == 0
        && header[5] == 2
        && header[26..28] == [1, 19]
        && &header[28..36] == b"OpusHead"
        && header[36..38] == [1, 1]
        && header[46] == 0
}

async fn prepare_message(
    client: &Client,
    root: &FsPath,
    request: MessageRequest,
) -> Result<wa::Message, ApiError> {
    let MessageRequest {
        kind,
        chat_id,
        text,
        path,
        expected_sha256,
        mimetype,
        filename,
        ..
    } = request;
    let status = chat_id == "status@broadcast";
    let media = match kind {
        Kind::Image => MediaType::Image,
        Kind::Document => MediaType::Document,
        Kind::Video => MediaType::Video,
        Kind::Audio => MediaType::Audio,
        Kind::Voice => {
            if text.is_some() {
                return Err(error(
                    StatusCode::BAD_REQUEST,
                    "Voice notes do not support a caption",
                ));
            }
            MediaType::Audio
        }
        Kind::Text => {
            if path.is_some()
                || expected_sha256.is_some()
                || mimetype.is_some()
                || filename.is_some()
            {
                return Err(error(
                    StatusCode::BAD_REQUEST,
                    "text requests do not contain attachment fields",
                ));
            }
            return text
                .filter(|text| !text.trim().is_empty())
                .map(wa::Message::text)
                .ok_or_else(|| error(StatusCode::BAD_REQUEST, "nonempty text is required"));
        }
    };
    let path = path.ok_or_else(|| {
        error(
            StatusCode::BAD_REQUEST,
            "an outbound spool file is required",
        )
    })?;
    let expected = expected_sha256
        .filter(|hash| {
            hash.len() == 64
                && hash
                    .bytes()
                    .all(|ch| ch.is_ascii_digit() || (b'a'..=b'f').contains(&ch))
        })
        .ok_or_else(|| error(StatusCode::BAD_REQUEST, "an attachment SHA-256 is required"))?;
    let mime = mimetype
        .filter(|mime| match kind {
            Kind::Voice => mime == STATUS_VOICE_MIME,
            _ => {
                mime.is_ascii()
                    && !mime.contains(char::is_whitespace)
                    && mime
                        .split_once('/')
                        .is_some_and(|(ty, sub)| !ty.is_empty() && !sub.is_empty())
                    && (!matches!(kind, Kind::Image) || mime.starts_with("image/"))
                    && (!matches!(kind, Kind::Video) || mime.starts_with("video/"))
                    && (!matches!(kind, Kind::Audio) || mime.starts_with("audio/"))
            }
        })
        .ok_or_else(|| error(StatusCode::BAD_REQUEST, "invalid attachment MIME type"))?;
    if matches!(kind, Kind::Document)
        && !filename.as_deref().is_some_and(|name| {
            !name.trim().is_empty()
                && !name.contains(['/', '\\'])
                && !name.contains(char::is_control)
        })
    {
        return Err(error(
            StatusCode::BAD_REQUEST,
            "a document filename is required",
        ));
    }
    let root = root.to_path_buf();
    let (file, info) = tokio::task::spawn_blocking(move || {
        spool_attachment(&root, &path, &expected, media, matches!(kind, Kind::Voice))
    })
    .await
    .map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "could not prepare attachment",
        )
    })??;
    let upload = client.upload_stream(file, info, media).await.map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "WhatsApp media upload failed before send",
        )
    })?;
    Ok(match kind {
        Kind::Image => {
            let mut options = ImageOptions::default().with_mimetype(mime);
            options.caption = text;
            media::image_message(upload, options)
        }
        Kind::Document => {
            let mut options = DocumentOptions::default().with_mimetype(mime);
            options.file_name = filename;
            options.caption = text;
            media::document_message(upload, options)
        }
        Kind::Video => {
            let mut options = VideoOptions::default().with_mimetype(mime);
            options.caption = text;
            media::video_message(upload, options)
        }
        Kind::Voice => {
            let mut message = media::audio_message(upload, AudioOptions::default().with_ptt(true));
            if status && let Some(audio) = message.audio_message.as_option_mut() {
                audio.background_argb = Some(STATUS_BACKGROUND);
            }
            message
        }
        Kind::Audio => {
            let mut options = AudioOptions::default();
            options.mimetype = Some(mime);
            media::audio_message(upload, options)
        }
        Kind::Text => unreachable!("text does not upload media"),
    })
}

async fn matches_chat(client: &Client, destination: &Jid, from: Option<&str>) -> bool {
    let Some(from) = from else { return true };
    if from == "s.whatsapp.net" || from == destination.to_string() {
        return true;
    }
    let Ok(sender) = from.parse::<Jid>() else {
        return false;
    };
    let sender = sender.to_non_ad();
    if sender == *destination {
        return true;
    }
    if !(destination.is_pn() && sender.is_lid() || destination.is_lid() && sender.is_pn()) {
        return false;
    }
    client
        .get_lid_pn_entry(destination)
        .await
        .ok()
        .flatten()
        .is_some_and(|mapping| {
            if sender.is_lid() {
                mapping.lid.as_ref() == sender.user.as_str()
            } else {
                mapping.phone_number.as_ref() == sender.user.as_str()
            }
        })
}

fn classify_ack(
    id: &MessageId,
    chat: &Jid,
    audience: &[String],
    ack: &whatsapp_rust::OwnedNodeRef,
) -> Result<Receipt, ApiError> {
    let node = ack.get();
    let mut attrs = ack.attrs();
    if ack.tag() != "ack"
        || attrs.optional_string("class").as_deref() != Some("message")
        || attrs.optional_string("id").as_deref() != Some(id.as_str())
    {
        return Err(error(
            StatusCode::BAD_GATEWAY,
            "WhatsApp acknowledgment did not match",
        ));
    }
    let error_attr = node.get_attr("error");
    let error_node = node.get_optional_child_by_tag(&["error"]);
    Ok(Receipt {
        message_id: id.to_string(),
        chat_id: chat.to_string(),
        audience: audience.to_vec(),
        status: if error_attr.is_some() || error_node.is_some() {
            Outcome::Rejected
        } else {
            Outcome::Accepted
        },
        rejection_status: error_attr
            .or_else(|| error_node.and_then(|child| child.get_attr("code")))
            .and_then(|attr| attr.as_str().parse().ok()),
    })
}

fn post_result(receipt: Receipt) -> Response {
    if receipt.status == Outcome::Rejected {
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "WhatsApp rejected the message",
                "message_id": receipt.message_id,
                "status": receipt.status,
                "rejection_status": receipt.rejection_status,
            })),
        )
            .into_response()
    } else {
        Json(json!({"message_id": receipt.message_id, "status": receipt.status})).into_response()
    }
}

pub(super) async fn send_message(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    body: Result<Json<MessageRequest>, JsonRejection>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    let Json(body) = body.map_err(|_| error(StatusCode::BAD_REQUEST, "invalid message request"))?;
    check_configured_bot(&bridge, &body.bot_number)?;
    let id = validate_id(&body.message_id)?;
    let (chat, audience) = if body.chat_id == "status@broadcast" {
        if matches!(body.kind, Kind::Document | Kind::Audio) {
            return Err(error(
                StatusCode::BAD_REQUEST,
                "documents and audio files cannot be posted as Status",
            ));
        }
        (
            Jid::status_broadcast(),
            validate_audience(body.audience.as_deref())?,
        )
    } else {
        if body.audience.is_some() {
            return Err(error(
                StatusCode::BAD_REQUEST,
                "Status fields require a Status destination",
            ));
        }
        (validate_chat(&body.chat_id)?, Vec::new())
    };
    let requested_audience = audience.iter().map(ToString::to_string).collect::<Vec<_>>();
    let client = require_owner_transport(&bridge)?;
    let root = state_root()?;
    let directory = receipt_directory(&root, &body.bot_number);
    let previous = load_receipt(&directory, &id).map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "WhatsApp send receipts are unavailable",
        )
    })?;
    if let Some(previous) = previous {
        if previous.chat_id != body.chat_id || previous.audience != requested_audience {
            return Err(error(
                StatusCode::CONFLICT,
                "message ID belongs to a different WhatsApp destination or Status audience",
            ));
        }
        return Ok(post_result(previous));
    }

    let status_text = chat.is_status_broadcast() && matches!(body.kind, Kind::Text);
    let recipients = if chat.is_status_broadcast() {
        Some(status_recipients(client, audience).await?)
    } else {
        None
    };
    let message = prepare_message(client, &root, body).await?;
    submit_message(
        &bridge,
        directory,
        id,
        chat,
        requested_audience,
        message,
        recipients.as_deref(),
        status_text,
    )
    .await
    .map(post_result)
}

pub(super) async fn forward_message(
    bridge: &Bridge,
    bot_number: &str,
    chat: Jid,
    id: MessageId,
    source: &Jid,
    source_id: &str,
) -> ApiResult {
    let directory = receipt_directory(&state_root()?, bot_number);
    if let Some(previous) = load_receipt(&directory, &id).map_err(|_| {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "WhatsApp send receipts are unavailable",
        )
    })? {
        if previous.chat_id != chat.to_string() || !previous.audience.is_empty() {
            return Err(error(
                StatusCode::CONFLICT,
                "message ID belongs to a different WhatsApp destination",
            ));
        }
        return Ok(post_result(previous));
    }
    let message = bridge
        .control_inbox
        .as_ref()
        .and_then(|inbox| inbox.find(source, source_id))
        .and_then(|record| {
            super::control::forwardable(&record.message)
                .map(|(message, _)| message.prepare_for_forward())
        })
        .ok_or_else(|| {
            error(
                StatusCode::BAD_REQUEST,
                "The source message is not available in the recent messages for this chat",
            )
        })?;
    submit_message(
        bridge,
        directory,
        id,
        chat,
        Vec::new(),
        message,
        None,
        false,
    )
    .await
    .map(post_result)
}

pub(crate) async fn send_provider_notice(bridge: &Bridge, call_id: &str, message: String) -> NoticeDm {
    let Some(bot) = bridge.cfg.expected_bot.as_deref() else {
        return NoticeDm::Unavailable;
    };
    if require_owner_transport(bridge).is_err() {
        return NoticeDm::Unavailable;
    }
    let (Ok(root), Ok(id)) = (
        state_root(),
        validate_id(&format!("3EB0{}A39F", call_id.to_ascii_uppercase())),
    ) else {
        return NoticeDm::Unavailable;
    };
    let directory = receipt_directory(&root, bot);
    let chat = Jid::pn(bridge.cfg.owner.as_str());
    let receipt = match load_receipt(&directory, &id) {
        Ok(Some(receipt)) if receipt.chat_id == chat.to_string() && receipt.audience.is_empty() => {
            Ok(receipt)
        }
        Ok(None) => {
            submit_message(
                bridge,
                directory,
                id,
                chat,
                Vec::new(),
                wa::Message::text(message),
                None,
                false,
            )
            .await
        }
        _ => return NoticeDm::Unconfirmed,
    };
    match receipt.map(|receipt| receipt.status) {
        Ok(Outcome::Accepted) => NoticeDm::Accepted,
        Ok(Outcome::Rejected) => NoticeDm::Rejected,
        _ => NoticeDm::Unconfirmed,
    }
}

async fn submit_message(
    bridge: &Bridge,
    directory: PathBuf,
    id: MessageId,
    chat: Jid,
    requested_audience: Vec<String>,
    message: wa::Message,
    recipients: Option<&[Jid]>,
    status_text: bool,
) -> Result<Receipt, ApiError> {
    let client = require_owner_transport(bridge)?;
    let ack = client.wait_for_node(
        NodeFilter::tag("ack")
            .attr("class", "message")
            .attr("id", id.as_str()),
    );
    let (receipt, sent) = tokio::time::timeout(Duration::from_secs(10), async {
        let uncertain = || {
            error(
                StatusCode::BAD_GATEWAY,
                "WhatsApp send outcome is unconfirmed",
            )
        };
        let sent = if let Some(recipients) = recipients {
            let status = client.status();
            let options = StatusSendOptions::default()
                .with_privacy(StatusPrivacySetting::AllowList)
                .with_message_id(id.clone())
                .with_device_freshness(Freshness::Refresh);
            if status_text {
                status
                    .send_text(
                        message.conversation.as_deref().ok_or_else(uncertain)?,
                        STATUS_BACKGROUND,
                        wa::message::extended_text_message::FontType::SYSTEM,
                        recipients,
                        options,
                    )
                    .await
            } else {
                status.send_raw(message, recipients, options).await
            }
        } else {
            let request = SendRequest::new(&chat, message)
                .with_options(SendOptions::default().with_message_id(id.clone()));
            client.send(request).await
        }
        .map_err(|_| uncertain())?;
        if sent.message_id != id || (chat.is_status_broadcast() && sent.to != chat) {
            return Err(uncertain());
        }
        let ack = ack.await.map_err(|_| uncertain())?;
        let from = ack.attrs().optional_string("from");
        if !matches_chat(client, &sent.to, from.as_deref()).await {
            return Err(uncertain());
        }
        Ok::<_, ApiError>((classify_ack(&id, &chat, &requested_audience, &ack)?, sent))
    })
    .await
    .map_err(|_| {
        error(
            StatusCode::GATEWAY_TIMEOUT,
            "WhatsApp send outcome is unconfirmed",
        )
    })??;
    let receipt = tokio::task::spawn_blocking(move || {
        persist_receipt(&directory, &id, &receipt)?;
        Ok::<_, io::Error>(receipt)
    })
    .await
    .ok()
    .and_then(Result::ok)
    .ok_or_else(|| {
        error(
            StatusCode::BAD_GATEWAY,
            "WhatsApp receipt could not be stored",
        )
    })?;
    if receipt.status == Outcome::Accepted
        && !chat.is_status_broadcast()
        && let (Some(inbox), Some(sender)) = (&bridge.control_inbox, client.pn())
    {
        inbox.remember_sent(chat, sender, sent);
    }
    Ok(receipt)
}

pub(super) async fn get_receipt(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Path(message_id): Path<String>,
    Query(query): Query<ReceiptQuery>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    check_configured_bot(&bridge, &query.bot_number)?;
    let id = validate_id(&message_id)?;
    let receipt = load_receipt(&receipt_directory(&state_root()?, &query.bot_number), &id)
        .map_err(|_| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "WhatsApp send receipts are unavailable",
            )
        })?
        .unwrap_or(Receipt {
            message_id,
            chat_id: String::new(),
            audience: Vec::new(),
            status: Outcome::Unknown,
            rejection_status: None,
        });
    Ok(post_result(receipt))
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{PermissionsExt, symlink};
    use whatsapp_rust::{NodeBuilder, OwnedNodeRef, wacore_binary};

    use super::*;

    const ID: &str = "3EB0TESTSEND00000001";
    const CONTENT_HASH: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

    #[test]
    fn accepts_only_whatsapp_chat_destinations_and_stable_ids() {
        for chat in [
            "628123456789@s.whatsapp.net",
            "1355@lid",
            "1203630123456@g.us",
        ] {
            assert!(validate_chat(chat).is_ok());
        }
        for chat in [
            "628123456789",
            "628123456789:2@s.whatsapp.net",
            "status@broadcast",
            "s.whatsapp.net",
        ] {
            assert!(validate_chat(chat).is_err());
        }
        assert!(validate_id(ID).is_ok());
        assert!(validate_id("../path/file").is_err());
    }

    #[test]
    fn status_requires_a_nonempty_canonical_user_audience() {
        for invalid in [
            "1203630123456@g.us",
            "status@broadcast",
            "628123456789:2@s.whatsapp.net",
            "s.whatsapp.net",
            "0@lid",
            "*",
        ] {
            assert!(validate_audience(Some(&["111@lid".into(), invalid.into()])).is_err());
        }
        assert!(validate_audience(None).is_err());
        assert!(validate_audience(Some(&[])).is_err());
        let selected = ["628123456789@s.whatsapp.net", "111@lid", "111@lid"].map(String::from);
        let recipients = validate_audience(Some(&selected)).unwrap();
        assert_eq!(
            recipients
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>(),
            ["111@lid", "628123456789@s.whatsapp.net"]
        );
    }

    #[test]
    fn voice_status_rejects_stereo_or_non_opus_audio() {
        let mut header = [0; 47];
        assert!(!is_mono_opus_head(&header));
        header[..4].copy_from_slice(b"OggS");
        header[5] = 2;
        header[26..28].copy_from_slice(&[1, 19]);
        header[28..36].copy_from_slice(b"OpusHead");
        header[36..38].copy_from_slice(&[1, 1]);
        assert!(is_mono_opus_head(&header));
        header[37] = 2;
        assert!(!is_mono_opus_head(&header));
    }

    fn ack(class: &str, id: &str, rejected: bool) -> OwnedNodeRef {
        let mut builder = NodeBuilder::new("ack").attr("class", class).attr("id", id);
        if rejected {
            builder = builder.attr("error", "479");
        }
        let packed = wacore_binary::marshal::marshal(&builder.build()).unwrap();
        OwnedNodeRef::new(packed[1..].to_vec()).unwrap()
    }

    #[test]
    fn server_ack_must_match_the_message_and_survives_receipt_reopen() {
        let id = validate_id(ID).unwrap();
        let chat = validate_chat("628123456789@s.whatsapp.net").unwrap();
        assert!(classify_ack(&id, &chat, &[], &ack("call", ID, false)).is_err());
        assert!(classify_ack(&id, &chat, &[], &ack("message", "someone-else", false)).is_err());
        let accepted = classify_ack(&id, &chat, &[], &ack("message", ID, false)).unwrap();
        let rejected_id = validate_id("3EB0OTHERTEST00000001").unwrap();
        let rejected = classify_ack(
            &rejected_id,
            &chat,
            &[],
            &ack("message", rejected_id.as_str(), true),
        )
        .unwrap();
        assert_eq!(accepted.status, Outcome::Accepted);
        assert_eq!(rejected.rejection_status, Some(479));
        let root = tempfile::tempdir().unwrap();
        let directory = receipt_directory(root.path(), "15550100001");
        assert!(load_receipt(&directory, &id).unwrap().is_none());
        persist_receipt(&directory, &id, &accepted).unwrap();
        persist_receipt(&directory, &id, &accepted).unwrap();
        assert_eq!(load_receipt(&directory, &id).unwrap(), Some(accepted));
        persist_receipt(&directory, &rejected_id, &rejected).unwrap();
        assert_eq!(
            post_result(load_receipt(&directory, &rejected_id).unwrap().unwrap()).status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(
            directory.join(ID).metadata().unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(
            load_receipt(&receipt_directory(root.path(), "15550100002"), &id)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn status_ack_persists_the_reviewed_audience_under_one_id() {
        let id = validate_id(ID).unwrap();
        let audience = ["111@lid", "628123456789@s.whatsapp.net"].map(String::from);
        let receipt = classify_ack(
            &id,
            &Jid::status_broadcast(),
            &audience,
            &ack("message", ID, false),
        )
        .unwrap();
        let root = tempfile::tempdir().unwrap();
        let directory = receipt_directory(root.path(), "15550100001");
        persist_receipt(&directory, &id, &receipt).unwrap();
        let saved = load_receipt(&directory, &id).unwrap().unwrap();
        assert_eq!(saved.audience, audience);
        assert_eq!(saved.chat_id, "status@broadcast");
        assert_eq!(saved.status, Outcome::Accepted);
    }

    #[test]
    fn only_exact_regular_spooled_bytes_are_staged_for_upload() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let spool = root.join("outbound");
        fs::create_dir(&spool).unwrap();
        let selected = spool.join("upload.txt");
        let outside = root.join("outside.txt");
        fs::write(&selected, b"abc").unwrap();
        fs::write(&outside, b"abc").unwrap();
        let link = spool.join("link.txt");
        symlink(&selected, &link).unwrap();
        for path in [&outside, &link] {
            assert!(
                spool_attachment(&root, path, CONTENT_HASH, MediaType::Document, false).is_err()
            );
        }
        for media in [MediaType::Image, MediaType::Document, MediaType::Audio] {
            let (staged, info) =
                spool_attachment(&root, &selected, CONTENT_HASH, media, false).unwrap();
            assert_eq!(info.file_length, 3);
            assert_eq!(
                staged.len() as usize,
                whatsapp_rust::wacore::upload::encrypted_len(3)
            );
            let mut ciphertext = vec![];
            staged
                .reader_from(0)
                .unwrap()
                .read_to_end(&mut ciphertext)
                .unwrap();
            let mut suffix = vec![];
            staged
                .reader_from(3)
                .unwrap()
                .read_to_end(&mut suffix)
                .unwrap();
            assert_eq!(suffix, ciphertext[3..]);
        }
        fs::write(&selected, b"different bytes").unwrap();
        assert!(
            spool_attachment(&root, &selected, CONTENT_HASH, MediaType::Document, false).is_err()
        );
        assert_eq!(fs::read_dir(&spool).unwrap().count(), 2);
    }
}
