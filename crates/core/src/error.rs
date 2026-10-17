//! Errors, worded for the person using the application.
//!
//! Everything a user can cause — a refused rule, a loan that is not active, a
//! journal on an old schema — is a variant with a sentence that can be shown as
//! is. Only `Db` and `Io` are failures of the machine rather than of the input.

/// The version of the journal schema this application reads and writes.
pub const SCHEMA_VERSION: i64 = 13;
/// The oldest journal it opens: 1.x's last version, which it brings up to
/// [`SCHEMA_VERSION`] on opening.
pub const OLDEST_SCHEMA_VERSION: i64 = 12;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),

    #[error("{0}")]
    Io(#[from] std::io::Error),

    #[error(
        "This journal is on schema version {found}. LoanMaster {app} opens journals from version \
         {expected} on — open it once in LoanMaster 1.x, which brings it up to date, then open it here."
    )]
    OutdatedSchema { found: i64, expected: i64, app: &'static str },

    #[error(
        "This journal was written by a newer LoanMaster (schema version {found}); LoanMaster {app} \
         reads up to version {newest}. Install the newer LoanMaster to open it."
    )]
    NewerSchema { found: i64, newest: i64, app: &'static str },

    #[error("This file is not a LoanMaster journal.")]
    NotAJournal,

    #[error("Member {0} not found")]
    MemberNotFound(i64),

    #[error("Loan '{0}' not found")]
    LoanNotFound(String),

    #[error("Loan '{reference}' is not active (status: {status})")]
    LoanInactive { reference: String, status: String },

    #[error("{}", suspended_message(.reference, .until))]
    LoanSuspended { reference: String, until: Option<String> },

    #[error("Christmas withdrawals are locked until {0}")]
    ChristmasLocked(String),

    /// One or more business rules refused the operation. Each sentence stands
    /// on its own; they are joined for display.
    #[error("{}", .0.join(" "))]
    NotAllowed(Vec<String>),

    /// Input the operation cannot accept, explained.
    #[error("{0}")]
    Invalid(String),

    /// The user stopped a long operation before it finished.
    #[error("Cancelled")]
    Cancelled,
}

fn suspended_message(reference: &str, until: &Option<String>) -> String {
    match until {
        Some(date) => format!("Loan '{reference}' is suspended until {date}"),
        None => format!("Loan '{reference}' is suspended indefinitely"),
    }
}

impl Error {
    /// True when the message is meant for the user as written — a refusal,
    /// not a fault. The UI shows these plainly and logs the rest.
    pub fn is_user_facing(&self) -> bool {
        !matches!(self, Error::Db(_) | Error::Io(_))
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Error::Invalid(message.into())
    }
}

pub type Result<T> = std::result::Result<T, Error>;
