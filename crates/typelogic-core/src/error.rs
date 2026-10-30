//! Error kinds mirror the Python exception classes raised by the reference implementation.
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// `ValueError`
    Value,
    /// `RuntimeError`
    Runtime,
    /// `OverflowError`
    Overflow,
    /// `TypeError`
    Type,
    /// The input cannot be represented by the core (for example a lone UTF-16 surrogate);
    /// bindings fall back to the Python reference implementation.
    Unsupported,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreError {
    pub kind: ErrorKind,
    pub message: String,
}

impl CoreError {
    pub fn value(message: impl Into<String>) -> Self { Self { kind: ErrorKind::Value, message: message.into() } }
    pub fn runtime(message: impl Into<String>) -> Self { Self { kind: ErrorKind::Runtime, message: message.into() } }
    pub fn overflow(message: impl Into<String>) -> Self { Self { kind: ErrorKind::Overflow, message: message.into() } }
    pub fn type_error(message: impl Into<String>) -> Self { Self { kind: ErrorKind::Type, message: message.into() } }
    pub fn unsupported(message: impl Into<String>) -> Self { Self { kind: ErrorKind::Unsupported, message: message.into() } }

    pub fn kind_name(&self) -> &'static str {
        match self.kind {
            ErrorKind::Value => "ValueError",
            ErrorKind::Runtime => "RuntimeError",
            ErrorKind::Overflow => "OverflowError",
            ErrorKind::Type => "TypeError",
            ErrorKind::Unsupported => "Unsupported",
        }
    }
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "{}: {}", self.kind_name(), self.message) }
}

impl std::error::Error for CoreError {}

pub type Result<T> = std::result::Result<T, CoreError>;
