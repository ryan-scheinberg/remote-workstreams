// WebAuthn ceremonies. Pairing (once per device): PIN to /api/pair/start, a
// Face ID registration, attestation to /api/pair/finish. Login (every app
// open): /api/login/start, a Face ID assertion, /api/login/finish. Both return
// a session token the caller holds in memory only — never in storage.

export class PairError extends Error {}

export function hasWebAuthn() {
  return Boolean(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create);
}

function b64urlToBuf(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function bufToB64url(buf) {
  let bin = "";
  for (const b of new Uint8Array(buf)) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function post(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Server sends WebAuthn JSON conventions (base64url strings); the browser API
// wants ArrayBuffers for challenge / user.id / credential ids.
function decodeCreationOptions(options) {
  const o = structuredClone(options.publicKey || options);
  o.challenge = b64urlToBuf(o.challenge);
  if (o.user) o.user.id = b64urlToBuf(o.user.id);
  for (const cred of o.excludeCredentials || []) cred.id = b64urlToBuf(cred.id);
  return o;
}

function decodeRequestOptions(options) {
  const o = structuredClone(options.publicKey || options);
  o.challenge = b64urlToBuf(o.challenge);
  for (const cred of o.allowCredentials || []) cred.id = b64urlToBuf(cred.id);
  return o;
}

// RegistrationResponseJSON, per the same conventions, for /api/pair/finish.
function encodeAttestation(cred) {
  const r = cred.response;
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || null,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      attestationObject: bufToB64url(r.attestationObject),
      transports: r.getTransports ? r.getTransports() : [],
    },
  };
}

// AuthenticationResponseJSON for /api/login/finish.
function encodeAssertion(cred) {
  const r = cred.response;
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || null,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      authenticatorData: bufToB64url(r.authenticatorData),
      signature: bufToB64url(r.signature),
      userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
    },
  };
}

function requireWebAuthn() {
  if (!hasWebAuthn()) {
    throw new PairError("This browser has no WebAuthn support. Open the app in Safari on iOS 16 or later.");
  }
}

export async function pairDevice(pin) {
  requireWebAuthn();

  const startRes = await post("/api/pair/start", { pin });
  if (startRes.status === 403) throw new PairError("Wrong PIN.");
  if (!startRes.ok) throw new PairError(`Pairing failed (HTTP ${startRes.status}).`);
  const { pairing_id, registration_options } = await startRes.json();

  let cred;
  try {
    cred = await navigator.credentials.create({ publicKey: decodeCreationOptions(registration_options) });
  } catch (err) {
    if (err.name === "NotAllowedError") throw new PairError("Face ID was cancelled or timed out. Try again.");
    throw new PairError(`Face ID failed: ${err.message}`);
  }
  if (!cred) throw new PairError("Face ID returned no credential. Try again.");

  const finishRes = await post("/api/pair/finish", {
    pairing_id,
    attestation: encodeAttestation(cred),
  });
  if (finishRes.status === 403) throw new PairError("The server rejected this pairing. Restart it on the Mac.");
  if (!finishRes.ok) throw new PairError(`Pairing failed (HTTP ${finishRes.status}).`);
  const { session_token } = await finishRes.json();
  if (!session_token) throw new PairError("The server returned no session token.");
  return session_token;
}

export async function loginDevice() {
  requireWebAuthn();

  const startRes = await post("/api/login/start", {});
  if (!startRes.ok) throw new PairError(`Login failed (HTTP ${startRes.status}).`);
  const { login_id, authentication_options } = await startRes.json();

  let cred;
  try {
    cred = await navigator.credentials.get({ publicKey: decodeRequestOptions(authentication_options) });
  } catch (err) {
    if (err.name === "NotAllowedError") throw new PairError("Face ID was cancelled or found no passkey. Pair this device if it's new.");
    throw new PairError(`Face ID failed: ${err.message}`);
  }
  if (!cred) throw new PairError("Face ID returned no passkey. Pair this device first.");

  const finishRes = await post("/api/login/finish", {
    login_id,
    assertion: encodeAssertion(cred),
  });
  if (finishRes.status === 403) throw new PairError("This passkey is no longer valid. Pair this device again.");
  if (!finishRes.ok) throw new PairError(`Login failed (HTTP ${finishRes.status}).`);
  const { session_token } = await finishRes.json();
  if (!session_token) throw new PairError("The server returned no session token.");
  return session_token;
}
