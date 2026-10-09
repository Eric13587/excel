//! LoanMaster's core: the journal, and everything that may be done to it.
//!
//! No user interface lives here. The application crate draws screens; this
//! crate decides what a loan owes, what a fund holds, and what is allowed.

pub mod arrears;
pub mod dates;
pub mod db;
pub mod error;
pub mod funds;
pub mod grid;
pub mod loans;
pub mod membership;
pub mod money;
pub mod policy;
pub mod reports;
pub mod statements;
pub mod undo;

pub use db::{Journal, OpenOptions, OpenReport};
pub use error::{Error, Result, SCHEMA_VERSION};
