//! On Windows, give the executable its icon and its name in Explorer.

fn main() {
    println!("cargo:rerun-if-changed=../../resources/icon.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let mut resource = winresource::WindowsResource::new();
    resource
        .set_icon("../../resources/icon.ico")
        .set("ProductName", "LoanMaster")
        .set("FileDescription", "LoanMaster: loan, savings and fund management");
    if let Err(e) = resource.compile() {
        // The program works without it; only Explorer's icon is missing.
        println!("cargo:warning=The Windows icon could not be embedded: {e}");
    }
}
