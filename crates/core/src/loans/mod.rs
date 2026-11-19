//! Loans: the event model, its projection into the ledger, and every
//! operation the application performs on a loan.

pub mod deletions;
pub mod derivation;
pub mod edits;
pub mod events;
pub mod guards;
pub mod projection;
pub mod service;
pub mod suspensions;
