//! Loopback HTTP API used by the Hermes plugin.

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use whatsapp_rust::NodeFilter;
use whatsapp_rust::prelude::*;

use crate::calls::Bridge;
use crate::owner_chat;

pub(crate) mod control;
mod outbound;
pub(crate) use outbound::send_provider_notice;

pub(crate) use control::contacts::subscribe as subscribe_control_contacts;
mod control_events;

type ApiError = (StatusCode, Json<serde_json::Value>);
type ApiResult = Result<Response, ApiError>;

fn error(status: StatusCode, msg: impl Into<String>) -> ApiError {
    (status, Json(json!({"error": msg.into()})))
}

fn authorize(bridge: &Bridge, headers: &HeaderMap) -> Result<(), ApiError> {
    let expected = format!("Bearer {}", bridge.cfg.token);
    match headers.get("authorization").and_then(|v| v.to_str().ok()) {
        Some(got) if constant_time_eq(got.as_bytes(), expected.as_bytes()) => Ok(()),
        _ => Err(error(StatusCode::UNAUTHORIZED, "bad or missing token")),
    }
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

async fn health(State(bridge): State<Arc<Bridge>>, headers: HeaderMap) -> ApiResult {
    authorize(&bridge, &headers)?;
    let logged_in = owner_chat::account_ready(&bridge);
    let verified_bot = bridge.cfg.expected_bot.as_deref().filter(|_| logged_in);
    Ok(Json(json!({
        "ok": true,
        "version": env!("CARGO_PKG_VERSION"),
        "logged_in": logged_in,
        "bot": verified_bot,
        "owner": verified_bot.map(|_| &bridge.cfg.owner),
        "active_call": bridge.calls.active(),
        "last_provider_failure": bridge.calls.last_provider_failure(),
        "hermes_connected": !bridge.cfg.hermes_api_key.is_empty(),
    }))
    .into_response())
}

#[derive(Deserialize)]
struct StartCall {
    reason: String,
    #[serde(default)]
    details: String,
}

async fn start_call(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Json(body): Json<StartCall>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    let reason = body.reason.trim().to_string();
    if reason.is_empty() {
        return Err(error(StatusCode::BAD_REQUEST, "reason is required"));
    }
    if !owner_chat::account_ready(&bridge) {
        return Err(error(
            StatusCode::SERVICE_UNAVAILABLE,
            "not connected to WhatsApp",
        ));
    }
    let record = bridge
        .begin_outbound(&reason)
        .map_err(|e| error(StatusCode::CONFLICT, e.to_string()))?;
    tokio::spawn(bridge.clone().run_outbound(
        record.id.clone(),
        reason,
        body.details.trim().to_string(),
    ));
    Ok((StatusCode::ACCEPTED, Json(record)).into_response())
}

#[derive(Deserialize)]
struct CallText {
    text: String,
}

/// A WhatsApp text from the owner, for the active call (the plugin's gateway hook sends it).
async fn call_text(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Json(body): Json<CallText>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    if bridge.cfg.expected_bot.is_some() && !owner_chat::account_ready(&bridge) {
        return Err(error(
            StatusCode::SERVICE_UNAVAILABLE,
            "selected WhatsApp bot unavailable",
        ));
    }
    let text = body.text.trim();
    if text.is_empty() || !bridge.deliver_text(text) {
        return Err(error(StatusCode::NOT_FOUND, "no active call takes texts"));
    }
    Ok(Json(json!({"delivered": true})).into_response())
}

#[derive(Deserialize)]
struct ListQuery {
    limit: Option<usize>,
}

async fn list_calls(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Query(q): Query<ListQuery>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    let calls = bridge.calls.recent(q.limit.unwrap_or(5).min(50));
    Ok(Json(json!({"calls": calls})).into_response())
}

async fn get_call(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    match bridge.calls.get(&id) {
        Some(record) => Ok(Json(record).into_response()),
        None => Err(error(StatusCode::NOT_FOUND, "no such call")),
    }
}

fn require_owner_transport(bridge: &Bridge) -> Result<&Client, ApiError> {
    if bridge.cfg.expected_bot.is_none() {
        return Err(error(
            StatusCode::NOT_FOUND,
            "owner DM transport is disabled",
        ));
    }
    bridge
        .client
        .as_deref()
        .filter(|_| owner_chat::account_ready(bridge))
        .ok_or_else(|| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "selected WhatsApp bot unavailable",
            )
        })
}

#[derive(Deserialize)]
struct OwnerRead {
    after: Option<String>,
}

async fn get_owner_messages(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Query(query): Query<OwnerRead>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    require_owner_transport(&bridge)?;
    let page = bridge
        .owner_inbox
        .as_ref()
        .unwrap()
        .poll(query.after.as_deref())
        .await
        .map_err(|()| {
            error(
                StatusCode::CONFLICT,
                "owner DM cursor is no longer available",
            )
        })?;
    require_owner_transport(&bridge)?;
    Ok(Json(page).into_response())
}

async fn owner_destination(bridge: &Bridge, client: &Client, chat_id: &str) -> Result<Jid, ApiError> {
    let destination = chat_id
        .parse::<Jid>()
        .ok()
        .filter(|jid| jid.to_non_ad().to_string() == chat_id)
        .ok_or_else(|| error(StatusCode::BAD_REQUEST, "non-device chat JID is required"))?;
    if !owner_chat::owner_address(client, &destination, &bridge.cfg.owner).await {
        return Err(error(
            StatusCode::FORBIDDEN,
            "destination is not the verified owner",
        ));
    }
    require_owner_transport(bridge)?;
    Ok(destination)
}

#[derive(Deserialize)]
struct OwnerChat {
    chat_id: String,
}

async fn send_owner_typing(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Json(body): Json<OwnerChat>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    let client = require_owner_transport(&bridge)?;
    let destination = owner_destination(&bridge, client, &body.chat_id).await?;
    client
        .chatstate()
        .send_composing(&destination)
        .await
        .map_err(|_| error(StatusCode::BAD_GATEWAY, "WhatsApp typing update unavailable"))?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

#[derive(Deserialize)]
struct OwnerReply {
    chat_id: String,
    text: String,
}

async fn send_owner_message(
    State(bridge): State<Arc<Bridge>>,
    headers: HeaderMap,
    Json(body): Json<OwnerReply>,
) -> ApiResult {
    authorize(&bridge, &headers)?;
    let client = require_owner_transport(&bridge)?;
    if body.text.trim().is_empty() {
        return Err(error(StatusCode::BAD_REQUEST, "nonempty text is required"));
    }
    let destination = owner_destination(&bridge, client, &body.chat_id).await?;
    let id = client.generate_message_id();
    let ack = client.wait_for_node(NodeFilter::tag("ack").attr("id", id.as_str()));
    let request = SendRequest::new(&destination, wa::Message::text(body.text))
        .with_options(SendOptions::default().with_message_id(id.clone()));
    let (confirmation, sent) = tokio::time::timeout(Duration::from_secs(10), async {
        let sent = client.send(request).await.map_err(|_| {
            error(
                StatusCode::BAD_GATEWAY,
                "WhatsApp send outcome is unconfirmed",
            )
        })?;
        if sent.message_id != id {
            return Err(error(
                StatusCode::BAD_GATEWAY,
                "WhatsApp send ID did not match",
            ));
        }
        let confirmation = ack
            .await
            .map_err(|_| error(StatusCode::BAD_GATEWAY, "WhatsApp send acknowledgment lost"))?;
        Ok((confirmation, sent))
    })
    .await
    .map_err(|_| {
        error(
            StatusCode::GATEWAY_TIMEOUT,
            "WhatsApp send outcome is unconfirmed",
        )
    })??;
    let mut attrs = confirmation.attrs();
    if attrs
        .optional_string("class")
        .is_some_and(|class| class != "message")
    {
        return Err(error(
            StatusCode::BAD_GATEWAY,
            "WhatsApp send acknowledgment did not match",
        ));
    }
    if attrs.optional_string("error").is_some() {
        return Err(error(
            StatusCode::BAD_GATEWAY,
            "WhatsApp rejected the message",
        ));
    }
    if let (Some(inbox), Some(sender)) = (&bridge.control_inbox, client.pn()) {
        inbox.remember_sent(destination, sender, sent);
    }
    Ok(Json(json!({"message_id": id.as_str()})).into_response())
}

pub async fn serve(bridge: Arc<Bridge>) -> anyhow::Result<()> {
    let addr = bridge.cfg.listen.clone();
    let app = Router::new()
        .route("/health", get(health))
        .route("/calls", get(list_calls).post(start_call))
        .route("/calls/active/text", post(call_text))
        .route("/calls/{id}", get(get_call))
        .route("/messages", post(outbound::send_message))
        .route("/messages/{message_id}", get(outbound::get_receipt))
        .route("/control", post(control::execute))
        .route("/control/messages", get(control_events::messages))
        .route("/control/media", get(control_events::media))
        .route(
            "/owner/messages",
            get(get_owner_messages).post(send_owner_message),
        )
        .route("/owner/typing", post(send_owner_typing))
        .with_state(bridge);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    log::info!("API listening on http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
