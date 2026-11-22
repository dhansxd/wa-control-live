use std::collections::HashSet;
use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;

use anyhow::Result;
use async_trait::async_trait;
use whatsapp_rust::{CallError, TokioRuntime};
use whatsapp_rust::voip::transport::RelayMediaChannelFactory;
use whatsapp_rust::voip::{RelayEndpointParams, RelayTransportFactory, RelayTransportProvider};
use whatsapp_rust::wacore::voip_control::relay_parse::{
    RelayData, WEB_CLIENT_RELAY_PORT, get_media_relay_endpoint, get_primary_ipv4_address,
};

const STANDARD_RELAY_PORT: u16 = 3478;

pub struct NativeRelayFallback;

#[async_trait]
impl RelayTransportProvider for NativeRelayFallback {
    async fn factory(&self, relay: &RelayEndpointParams) -> Result<Arc<dyn RelayTransportFactory>> {
        Ok(Arc::new(RelayMediaChannelFactory::new(
            native_factory_addr(relay.addr),
            Arc::new(TokioRuntime),
        )))
    }
}

fn native_factory_addr(mut advertised: SocketAddr) -> SocketAddr {
    // The native factory races its input port with 3480, and prefers 3480.
    // When 3480 is already selected it would otherwise open just one socket.
    if advertised.port() == WEB_CLIENT_RELAY_PORT {
        advertised.set_port(STANDARD_RELAY_PORT);
    }
    advertised
}

#[derive(Default)]
struct RelayOfferSummary {
    usable_ipv4_hosts: usize,
    selected_port: Option<u16>,
    selected_token_present: bool,
}

fn summarize_offer(relay: &RelayData) -> RelayOfferSummary {
    let has_token = |index: u32| {
        relay
            .relay_tokens
            .get(index as usize)
            .is_some_and(|token| !token.is_empty())
    };
    let hosts = relay
        .endpoints
        .iter()
        .filter(|endpoint| has_token(endpoint.token_id))
        .filter_map(get_primary_ipv4_address)
        .filter(|(_, port)| *port != 0)
        .filter_map(|(ip, _)| ip.parse::<Ipv4Addr>().ok())
        .collect::<HashSet<_>>();
    let selected = get_media_relay_endpoint(relay);
    RelayOfferSummary {
        usable_ipv4_hosts: hosts.len(),
        selected_port: selected
            .and_then(get_primary_ipv4_address)
            .map(|(_, port)| port),
        selected_token_present: selected.is_some_and(|endpoint| has_token(endpoint.token_id)),
    }
}

pub(super) fn log_inbound_offer_result(
    id: &str,
    relay: Option<&RelayData>,
    error: Option<&CallError>,
) {
    let summary = relay.map(summarize_offer).unwrap_or_default();
    let outcome = match error {
        None => "answered",
        Some(CallError::Connect(reason)) if reason.contains("dtls handshake timeout") => {
            "dtls_timeout"
        }
        Some(CallError::Connect(reason)) if reason.contains("call ended during relay connect") => {
            "ended_during_connect"
        }
        Some(CallError::Connect(reason))
            if reason.contains("did not connect within") || reason.contains("relay connect timed out") =>
        {
            "relay_timeout"
        }
        Some(CallError::Connect(_)) => "connect_failed",
        Some(CallError::CallEndedDuringSetup) => "ended_during_setup",
        Some(_) => "accept_failed",
    };
    log::info!(
        "call {id}: relay offer usable_ipv4_hosts={} selected_port={:?} selected_token_present={} outcome={outcome}",
        summary.usable_ipv4_hosts,
        summary.selected_port,
        summary.selected_token_present,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use whatsapp_rust::wacore::voip_control::relay_parse::{RelayAddress, RelayEndpoint};

    #[test]
    fn includes_standard_port_when_whatsapp_names_the_web_port() {
        let advertised = "127.0.0.1:3480".parse().unwrap();
        let alternate: SocketAddr = "127.0.0.1:3478".parse().unwrap();
        assert_eq!(native_factory_addr(advertised), alternate);
    }

    #[test]
    fn preserves_other_advertised_ports() {
        for endpoint in ["127.0.0.1:3478", "127.0.0.1:9000"] {
            let advertised = endpoint.parse().unwrap();
            assert_eq!(native_factory_addr(advertised), advertised);
        }
    }

    #[test]
    fn counts_distinct_usable_ipv4_hosts_in_the_offer() {
        let endpoint = |ip: &str, port, token_id| RelayEndpoint {
            token_id,
            addresses: vec![RelayAddress {
                protocol: 0,
                ipv4: Some(ip.into()),
                ipv6: None,
                port,
            }],
            ..Default::default()
        };
        let relay = RelayData {
            relay_tokens: vec![vec![1], vec![]],
            endpoints: vec![
                endpoint("192.0.2.1", 3478, 0),
                endpoint("192.0.2.1", 3480, 0),
                endpoint("192.0.2.2", 3480, 0),
                endpoint("192.0.2.3", 3480, 1),
                endpoint("192.0.2.4", 3480, 2),
                endpoint("invalid", 3480, 0),
                endpoint("192.0.2.5", 0, 0),
            ],
            ..Default::default()
        };
        let summary = summarize_offer(&relay);
        assert_eq!(summary.usable_ipv4_hosts, 2);
        assert_eq!(summary.selected_port, Some(3480));
        assert!(summary.selected_token_present);
    }
}
