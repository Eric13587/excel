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
    if !recipient.is_empty() {
        validate(recipient)?;
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

pub fn validate(address: &str) -> std::io::Result<()> {
    let valid = address.split_once('@').is_some_and(|(local, domain)| {
        !local.is_empty()
            && !domain.is_empty()
            && !domain.contains('@')
            && !address.starts_with('-')
            && address.is_ascii()
            && address
                .bytes()
                .all(|b| b.is_ascii_graphic() && !b",;<>:()[]\\".contains(&b))
    });
    if valid {
        Ok(())
    } else {
        Err(std::io::Error::other("Check the member's email address."))
    }
}

pub fn save_statement_draft(
    folder: &Path,
    id: i64,
    address: &str,
    subject: &str,
    attachment: &Path,
) -> std::io::Result<()> {
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    use std::io::Write;
    validate(address)?;
    let (mime, ext) = if attachment.extension().is_some_and(|ext| ext == "xlsx") {
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
        )
    } else {
        ("application/pdf", "pdf")
    };
    let mut temp = tempfile::NamedTempFile::new_in(folder)?;
    let subject = STANDARD.encode(subject.as_bytes());
    let boundary = "=_LoanMaster_Statement";
    write!(
        temp,
        "X-Unsent: 1\r\nTo: {address}\r\nSubject: =?UTF-8?B?{subject}?=\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"{boundary}\"\r\n\r\n--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nPlease find your member statement attached.\r\n\r\n--{boundary}\r\nContent-Type: {mime}\r\nContent-Disposition: attachment; filename=\"Statement-{id}.{ext}\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    )?;
    let encoded = STANDARD.encode(std::fs::read(attachment)?);
    for line in encoded.as_bytes().chunks(76) {
        temp.write_all(line)?;
        temp.write_all(b"\r\n")?;
    }
    write!(temp, "--{boundary}--\r\n")?;
    temp.flush()?;
    for sequence in 1.. {
        let name = if sequence == 1 {
            format!("Statement-{id}.eml")
        } else {
            format!("Statement-{id}-{sequence}.eml")
        };
        match temp.persist_noclobber(folder.join(name)) {
            Ok(_) => return Ok(()),
            Err(e) if e.error.kind() == std::io::ErrorKind::AlreadyExists => temp = e.file,
            Err(e) => return Err(e.error),
        }
    }
    unreachable!()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_draft_has_one_recipient_and_attachment_and_never_overwrites() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("Member.pdf");
        std::fs::write(&file, b"example").unwrap();
        for _ in 0..2 {
            save_statement_draft(dir.path(), 7, "demo@example.invalid", "Statement", &file)
                .unwrap();
        }
        let draft = std::fs::read_to_string(dir.path().join("Statement-7.eml")).unwrap();
        assert!(draft.contains("To: demo@example.invalid\r\n"));
        assert!(draft.contains("ZXhhbXBsZQ=="));
        assert!(dir.path().join("Statement-7-2.eml").is_file());
        assert!(validate("one@example.invalid;two@example.invalid").is_err());
        assert!(validate("one@example.invalid\r\nCc:another@example.invalid").is_err());
    }
}
