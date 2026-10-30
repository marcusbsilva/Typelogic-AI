//! TypeLogic AI core: the deterministic, model-independent half of the decision pipeline.
//!
//! Every function here is a byte-compatible port of a Python reference in `typelogic/`
//! (validation, canonical JSON, record fingerprints, Lite export, routing).
//! The neural backbone stays in PyTorch; only the final pointer readout is mirrored in [`readout`].
//! Bindings: `typelogic-py` (PyO3/maturin), `typelogic-node` (N-API), `typelogic-lua` (Lua 5.4),
//! `typelogic-cli` (JSON lines over stdio, for any language).

pub mod api;
pub mod error;
pub mod exports;
pub mod hash;
pub mod json;
pub mod pyfloat;
pub mod pysum;
pub mod readout;
pub mod router;
pub mod schema;
pub mod text;
mod unicode_tables;

pub use error::{CoreError, ErrorKind, Result};
pub use json::Value;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub use unicode_tables::UNICODE_VERSION;
