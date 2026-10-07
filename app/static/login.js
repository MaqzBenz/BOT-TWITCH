// Formulaire de connexion (script externe : la CSP interdit le JS inline)
document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("login-error");
  const btn = document.getElementById("login-submit");
  err.textContent = "";
  btn.disabled = true;
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      }),
    });
    if (r.ok) { location.href = "/"; return; }
    err.textContent = (await r.json().catch(() => ({}))).detail || "Erreur de connexion";
  } catch {
    err.textContent = "Serveur injoignable";
  } finally {
    btn.disabled = false;
  }
});
