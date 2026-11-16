use whatsapp_rust::download::DownloadLimits;
use whatsapp_rust::wacore::download::Downloadable;

use super::*;
use crate::control_inbox::ControlInbox;

#[derive(Deserialize)]
pub(super) struct InboxQuery {
    bot_number: String,
    cursor: Option<String>,
}

#[derive(Deserialize)]
pub(super) struct MediaQuery {
    bot_number: String,
    chat: String,
    id: String,
}

fn transport<'a>(
    bridge: &'a Bridge,
    headers: &HeaderMap,
    bot: &str,
) -> Result<(&'a Client, &'a ControlInbox), ApiError> {
    authorize(bridge, headers)?;
    if bridge.cfg.expected_bot.as_deref() != Some(bot) {
        return Err(error(StatusCode::FORBIDDEN, "WhatsApp bot does not match"));
    }
    let client = require_owner_transport(bridge)?;
    let inbox = bridge
        .control_inbox
        .as_ref()
        .ok_or_else(|| error(StatusCode::NOT_FOUND, "WhatsApp Control is unavailable"))?;
    Ok((client, inbox))
}

fn quoted_image(inbox: &ControlInbox, message: &InboundMessage) -> Option<InboundMessage> {
    let context = message.message.context_info()?;
    let quote = context.quoted_message.as_option()?;
    let chat = message.info.source.chat.to_non_ad();
    if quote.is_view_once()
        || !quote.get_base_message().image_message.is_set()
        || context
            .remote_jid
            .as_ref()
            .is_some_and(|jid| jid != &chat.to_string())
    {
        return None;
    }
    let id = context.stanza_id.as_deref()?;
    if id == message.info.id.as_str() {
        return None;
    }
    inbox
        .find(&chat, id)
        .filter(|original| original.message.get_base_message().image_message.is_set())
}

pub(super) async fn messages(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Query(query): Query<InboxQuery>,
) -> ApiResult {
    let (client, inbox) = transport(&bridge, &headers, &query.bot_number)?;
    let (cursor, messages) = inbox
        .poll(query.cursor.as_deref())
        .await
        .map_err(|_| error(StatusCode::CONFLICT, "WhatsApp message cursor expired"))?;
    let self_ids: Vec<String> = [client.pn(), client.lid()]
        .into_iter()
        .flatten()
        .map(|jid| jid.to_non_ad_string())
        .collect();
    let mut events = Vec::new();
    for message in messages {
        let info = &message.info;
        let source = &info.source;
        if source.is_from_me
            || info.is_offline
            || (!source.is_group
                && (source
                    .sender_alt
                    .as_ref()
                    .is_some_and(|jid| jid.is_pn() && jid.user == bridge.cfg.owner)
                    || owner_chat::owner_address(
                        client,
                        &source.chat.to_non_ad(),
                        &bridge.cfg.owner,
                    )
                    .await))
        {
            continue;
        }
        let base = message.message.get_base_message();
        let context = base.context_info();
        let text = base
            .text_content()
            .or_else(|| {
                base.image_message
                    .as_option()
                    .and_then(|image| image.caption.as_deref())
            })
            .unwrap_or_default();
        let media = if let Some(image) = base.image_message.as_option() {
            json!({"type":"image", "mime":image.mimetype.as_deref().unwrap_or("image/jpeg")})
        } else if let Some(audio) = base
            .audio_message
            .as_option()
            .filter(|audio| audio.ptt == Some(true))
        {
            json!({"type":"voice", "mime":audio.mimetype.as_deref().unwrap_or("audio/ogg")})
        } else if context
            .and_then(|ctx| ctx.quoted_message.as_option())
            .is_some_and(|quote| {
                !quote.is_view_once() && quote.get_base_message().image_message.is_set()
            })
        {
            let quoted = quoted_image(inbox, &message);
            let mime = quoted.as_ref().and_then(|original| {
                original
                    .message
                    .get_base_message()
                    .image_message
                    .as_option()
                    .and_then(|image| image.mimetype.as_deref())
            });
            json!({"type":"image", "quoted":true, "unavailable":quoted.is_none(), "mime":mime})
        } else {
            serde_json::Value::Null
        };
        if text.is_empty() && media.is_null() {
            continue;
        }
        let mentions: Vec<String> = context
            .into_iter()
            .flat_map(|ctx| &ctx.mentioned_jid)
            .filter_map(|jid| jid.parse::<Jid>().ok())
            .map(|jid| jid.to_non_ad_string())
            .collect();
        events.push(json!({
            "event": "message", "chat": source.chat.to_non_ad_string(), "id": info.id.as_str(),
            "sender": source.sender.to_non_ad_string(),
            "sender_alt": source.sender_alt.as_ref().map(Jid::to_non_ad_string),
            "text": text, "mentions": mentions, "self_ids": self_ids,
            "is_group": source.is_group, "chat_type": if source.is_group { "group" } else { "dm" },
            "from_me": false, "live": true, "media": media, "timestamp": info.timestamp.timestamp(),
        }));
    }
    Ok(Json(json!({"cursor":cursor, "messages":events})).into_response())
}

pub(super) async fn media(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Query(query): Query<MediaQuery>,
) -> ApiResult {
    let (client, inbox) = transport(&bridge, &headers, &query.bot_number)?;
    let chat = outbound::validate_chat(&query.chat)?;
    let message = inbox
        .find(&chat, &query.id)
        .ok_or_else(|| error(StatusCode::NOT_FOUND, "WhatsApp media is unavailable"))?;
    let direct = message.message.get_base_message();
    let audio = direct
        .audio_message
        .as_option()
        .filter(|audio| audio.ptt == Some(true));
    let quoted = if audio.is_none() && !direct.image_message.is_set() {
        quoted_image(inbox, &message)
    } else {
        None
    };
    let image = direct.image_message.as_option().or_else(|| {
        quoted.as_ref().and_then(|original| {
            original
                .message
                .get_base_message()
                .image_message
                .as_option()
        })
    });
    let (attachment, mime, maximum): (&dyn Downloadable, &str, u64) = if let Some(image) = image {
        (
            image,
            image.mimetype.as_deref().unwrap_or("image/jpeg"),
            8 * 1024 * 1024,
        )
    } else if let Some(audio) = audio {
        (
            audio,
            audio.mimetype.as_deref().unwrap_or("audio/ogg"),
            16 * 1024 * 1024,
        )
    } else {
        return Err(error(
            StatusCode::NOT_FOUND,
            "WhatsApp media is unavailable",
        ));
    };
    let mime = mime.split(';').next().unwrap_or_default().trim();
    let mime = axum::http::HeaderValue::from_str(mime)
        .map_err(|_| error(StatusCode::BAD_REQUEST, "Invalid WhatsApp attachment type"))?;
    let data = client
        .download(attachment, DownloadLimits::new(maximum + 4096, maximum))
        .await
        .map_err(|_| error(StatusCode::BAD_GATEWAY, "WhatsApp media download failed"))?;
    Ok(([(axum::http::header::CONTENT_TYPE, mime)], data).into_response())
}
