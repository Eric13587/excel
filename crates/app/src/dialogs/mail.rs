//! Ask the local mail application to compose a message with the exported file.
use crate::app::{Action, Notice};
use std::{path::Path, process::Command};

pub fn draft(actions: &mut Vec<Action>, recipient: &str, subject: &str, path: &Path) {
    let result = compose(recipient, subject, path);
    match result {
        Ok(()) => actions.push(Action::Status(format!(
            "Opened email draft for {}",
            path.display()
        ))),
        Err(e) => actions.push(Action::Notice(Notice::warning(
            "Open email draft",
            format!(
                "The document was saved to {}. The mail application could not open the draft: {e}",
                path.display()
            ),
        ))),
    }
}
fn compose(recipient: &str, subject: &str, path: &Path) -> std::io::Result<()> {
    if !recipient.is_empty()
        && (!recipient.contains('@')
            || recipient.starts_with('-')
            || recipient
                .chars()
                .any(|c| c.is_whitespace() || ",;<>".contains(c)))
    {
        return Err(std::io::Error::other("Check the member's email address."));
    }
    let path = std::path::absolute(path)?;
    #[cfg(windows)]
    let status=Command::new("powershell.exe").args(["-NoProfile","-NonInteractive","-Command",r#"$ErrorActionPreference='Stop'; $mail=(New-Object -ComObject Outlook.Application).CreateItem(0); $mail.To=$env:LM_TO; $mail.Subject=$env:LM_SUBJECT; [void]$mail.Attachments.Add($env:LM_FILE); $mail.Display()"#])
        .env("LM_TO",recipient).env("LM_SUBJECT",subject).env("LM_FILE",path).status()?;
    #[cfg(not(windows))]
    let status = {
        let mut cmd = Command::new("xdg-email");
        cmd.arg("--subject").arg(subject).arg("--attach").arg(path);
        if !recipient.is_empty() {
            cmd.arg(recipient);
        }
        cmd.status()?
    };
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other(format!(
            "Mail composer exited with {status}"
        )))
    }
}
