//! `d1-gateway-nats`'s error type (delta-one/CLAUDE.md: one `thiserror` enum per crate).

/// Failure modes for the NATS gateway: message conversion (`crate::convert`)
/// and the underlying `async-nats` client.
#[derive(Debug, thiserror::Error)]
pub enum NatsError {
    /// A `TargetPosition` arrived without its required `instrument` field.
    #[error("TargetPosition missing required field: {0}")]
    MissingField(&'static str),
    /// Failed to connect to the NATS server.
    #[error(transparent)]
    Connect(#[from] async_nats::ConnectError),
    /// Failed to publish a message.
    #[error(transparent)]
    Publish(#[from] async_nats::PublishError),
    /// Failed to subscribe to a subject.
    #[error(transparent)]
    Subscribe(#[from] async_nats::SubscribeError),
    /// A `TargetPosition` payload failed to decode as Protobuf.
    #[error(transparent)]
    Decode(#[from] prost::DecodeError),
}
