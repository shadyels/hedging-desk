//! d1-gateway-nats — see delta-one/CLAUDE.md for the crate's role and rules.

// prost emits cross-package references as `super::super::common::v1::Meta`
// (relative to each package's own module path), so the module nesting here
// must mirror the proto package paths (`hedging.common.v1`, `hedging.live.v1`)
// exactly, not just the flat output filenames.
#[allow(clippy::all, clippy::pedantic, clippy::nursery, missing_docs)]
pub mod pb {
    pub mod hedging {
        pub mod common {
            pub mod v1 {
                include!("gen/hedging.common.v1.rs");
            }
        }
        pub mod live {
            pub mod v1 {
                include!("gen/hedging.live.v1.rs");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::pb::hedging::{common::v1::Meta, live::v1::TargetPosition};

    #[test]
    fn generated_types_construct_and_cross_reference() {
        let target = TargetPosition {
            meta: Some(Meta {
                msg_id: "abc".to_string(),
                producer: "exo".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 7,
            ..Default::default()
        };
        assert_eq!(target.book_id, 7);
        assert_eq!(
            target.meta.as_ref().map(|m| m.producer.as_str()),
            Some("exo")
        );
    }
}
