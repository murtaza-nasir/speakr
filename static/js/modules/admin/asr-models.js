// Pure helpers for the merged "Transcription models" admin editor.
//
// The admin page renders with Vue from a CDN inside a server template, so the
// logic that needs a unit test lives here and the template calls it. Keep this
// module free of DOM access: it only maps connector schemas, form values and
// API payloads.

export const MASKED_SECRET = '***';

// Mirrors ENV_CONNECTOR_NAMES in src/services/asr_config.py: the connector
// part of an environment id, and of the default profile label.
export const ENV_CONNECTOR_NAMES = Object.freeze({
  openai_whisper: 'openai-whisper',
  openai_transcribe: 'openai-transcribe',
  asr_endpoint: 'whisper-asr',
  azure_openai_transcribe: 'azure-openai-transcribe',
  mistral: 'mistral',
  vibevoice: 'vibevoice',
  funasr: 'funasr',
  mossland: 'mossland',
  assemblyai: 'assemblyai',
  tencent_asr: 'tencent-asr',
  openasr: 'openasr',
  audiocpp: 'audiocpp',
});

// The fields every connector modal shows before the advanced ones.
export const COMMON_FIELDS = Object.freeze([
  'base_url', 'endpoint', 'api_key', 'secret_id', 'secret_key',
  'deployment_name', 'model',
]);

export const FIELD_LABELS = Object.freeze({
  base_url: 'Base URL',
  endpoint: 'Endpoint',
  api_key: 'API key',
  secret_id: 'Secret ID',
  secret_key: 'Secret key',
  deployment_name: 'Deployment name',
  api_version: 'API version',
  model: 'Model name',
  timeout: 'Request timeout (seconds)',
  diarize: 'Diarize by default',
  return_speaker_embeddings: 'Return speaker embeddings',
  poll_interval: 'Poll interval (seconds)',
  poll_timeout: 'Polling timeout (seconds)',
  stream_read_timeout: 'Stream read timeout (seconds)',
  disfluency_removal_enabled: 'Remove filler words',
  timestamp_alignment_enabled: 'Align timestamps',
  language_hints: 'Language hints',
  speaker_count: 'Speaker count',
  vocabulary_id: 'Vocabulary ID',
  channel_id: 'Channels',
  boost_param: 'Word boost strength',
  hotword_id: 'Hotword list ID',
  staging_prefix: 'Staging prefix',
  model_diarizes: 'Speaker labels in the output',
  max_duration_seconds: 'Longest audio per request (seconds)',
});

export function envConnectorName(connector) {
  const name = typeof connector === 'string' ? connector.trim().toLowerCase() : '';
  if (!name) return '';
  return ENV_CONNECTOR_NAMES[name] || name.replace(/_/g, '-');
}

export function defaultLabel(connector, model) {
  const slug = model === null || model === undefined ? '' : String(model).trim().toLowerCase();
  return `${envConnectorName(connector)}:${slug}`;
}

// A label that ends with a colon has its model part missing, which the
// environment snapshot produces when the environment names no model. Fill it
// with the chosen model ("whisper-asr:" + "small" -> "whisper-asr:small") and
// drop the colon when there is no model, so a saved label never ends in ":".
export function completeLabel(label, model) {
  const text = String(label === null || label === undefined ? '' : label).trim();
  if (!text.endsWith(':')) return text;
  const slug = model === null || model === undefined ? '' : String(model).trim().toLowerCase();
  return slug ? `${text}${slug}` : text.slice(0, -1);
}

export function fieldLabel(key) {
  if (FIELD_LABELS[key]) return FIELD_LABELS[key];
  return String(key || '')
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

export function schemaEntries(schema) {
  return Object.entries((schema && schema.properties) || {});
}

export function requiredFields(schema) {
  const required = (schema && schema.required) || [];
  return Array.isArray(required) ? required : [];
}

export function classifyFields(schema) {
  const common = [];
  let model = null;
  const advanced = [];
  for (const entry of schemaEntries(schema)) {
    const [key] = entry;
    if (key === 'model') {
      model = entry;
    } else if (COMMON_FIELDS.includes(key)) {
      common.push(entry);
    } else {
      advanced.push(entry);
    }
  }
  return { common, model, advanced };
}

export function secretFields(schema) {
  const names = [];
  for (const [key, property] of schemaEntries(schema)) {
    if (property && property.writeOnly) names.push(key);
  }
  return names;
}

export function arrayItemType(property) {
  const items = property && property.items;
  return (items && items.type) || 'string';
}

export function isSimpleArray(property) {
  return ['string', 'integer', 'number'].includes(arrayItemType(property));
}

export function schemaControl(property) {
  if (!property) return 'text';
  if (property.writeOnly) return 'secret';
  if (property.type === 'boolean') return 'boolean';
  if (property.type === 'integer' || property.type === 'number') return 'number';
  if (property.type === 'array') return isSimpleArray(property) ? 'list' : 'json';
  if (property.type === 'object') return 'json';
  if (Array.isArray(property.enum) && property.enum.length) return 'enum';
  return 'text';
}

function clone(value) {
  if (Array.isArray(value)) return value.slice();
  if (value && typeof value === 'object') return JSON.parse(JSON.stringify(value));
  return value;
}

export function schemaDefaults(schema) {
  const defaults = {};
  for (const [key, property] of schemaEntries(schema)) {
    if (property && property.default !== undefined) defaults[key] = clone(property.default);
  }
  return defaults;
}

export function emptyValue(property) {
  if (!property) return '';
  if (property.type === 'boolean') return false;
  if (property.type === 'array') return [];
  return '';
}

export function displayValue(property, value) {
  const control = schemaControl(property);
  if (control === 'boolean') return value === true;
  if (value === null || value === undefined) return '';
  if (control === 'list') {
    return Array.isArray(value) ? value.join(', ') : String(value);
  }
  if (control === 'json') {
    if (typeof value === 'string') return value;
    try {
      return JSON.stringify(value);
    } catch (error) {
      return '';
    }
  }
  return String(value);
}

// Form values are what the controls hold: booleans stay booleans, everything
// else is a string (arrays and objects included, as text).
export function formValuesFromConfig(schema, config) {
  const source = config || {};
  const values = {};
  for (const [key, property] of schemaEntries(schema)) {
    const has = Object.prototype.hasOwnProperty.call(source, key) && source[key] !== null
      && source[key] !== undefined;
    values[key] = has ? displayValue(property, source[key]) : emptyValue(property);
  }
  return values;
}

export function parseFieldValue(property, raw) {
  const control = schemaControl(property);
  if (control === 'boolean') {
    return { value: raw === true || raw === 'true' };
  }
  if (control === 'number') {
    if (raw === '' || raw === null || raw === undefined) return { omit: true };
    const number = typeof raw === 'number' ? raw : Number(String(raw).trim());
    if (!Number.isFinite(number)) return { error: 'not a number' };
    if (property.type === 'integer' && !Number.isInteger(number)) {
      return { error: 'not an integer' };
    }
    return { value: number };
  }
  if (control === 'enum' || control === 'secret' || control === 'text') {
    return { value: raw === null || raw === undefined ? '' : String(raw) };
  }
  if (control === 'list') {
    const text = raw === null || raw === undefined ? '' : String(raw);
    const parts = text.split(',').map((part) => part.trim()).filter((part) => part !== '');
    if (arrayItemType(property) === 'string') return { value: parts };
    const numbers = [];
    for (const part of parts) {
      const number = Number(part);
      if (!Number.isFinite(number)) return { error: `"${part}" is not a number` };
      if (arrayItemType(property) === 'integer' && !Number.isInteger(number)) {
        return { error: `"${part}" is not an integer` };
      }
      numbers.push(number);
    }
    return { value: numbers };
  }
  // JSON textarea for other arrays and objects.
  const text = raw === null || raw === undefined || raw === '' ? '' : String(raw);
  if (text === '') {
    return property.type === 'array' ? { value: [] } : { omit: true };
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { error: 'not valid JSON' };
  }
  if (property.type === 'array' && !Array.isArray(parsed)) {
    return { error: 'not a JSON array' };
  }
  if (property.type === 'object' && (Array.isArray(parsed) || parsed === null || typeof parsed !== 'object')) {
    return { error: 'not a JSON object' };
  }
  return { value: parsed };
}

// Build the `config` object the API stores, and whether the stored secrets
// must be cleared. Stored secrets are write-only: the form never receives
// them, so an empty (or masked) secret field keeps the stored value and is
// omitted from the payload. `clearSecrets` removes every secret, which only
// the "Remove the stored key" checkbox asks for.
export function buildConfigPayload(schema, values, options = {}) {
  const config = {};
  const clearSecrets = Boolean(options.clearSecrets);
  for (const [key, property] of schemaEntries(schema)) {
    const raw = values ? values[key] : undefined;
    if (property && property.writeOnly) {
      const text = raw === null || raw === undefined ? '' : String(raw).trim();
      if (text && text !== MASKED_SECRET) {
        config[key] = text;
      }
      continue;
    }
    const parsed = parseFieldValue(property, raw);
    if (parsed.error) {
      throw new Error(`${fieldLabel(key)}: ${parsed.error}`);
    }
    if (!parsed.omit) config[key] = parsed.value;
  }
  return { config, clearSecrets };
}

function normalizeUrl(value) {
  return String(value === null || value === undefined ? '' : value).trim().replace(/\/+$/, '');
}

// Whether a URL change drops stored secrets unless a new one is typed.
export function urlChangeDropsSecret(schema, storedConfig, values) {
  const secrets = secretFields(schema);
  if (!secrets.length) return false;
  const stored = storedConfig || {};
  const current = values || {};
  for (const field of secrets) {
    const typed = current[field] === null || current[field] === undefined
      ? '' : String(current[field]).trim();
    const storedValue = stored[field] === null || stored[field] === undefined
      ? '' : String(stored[field]);
    if (typed && typed !== MASKED_SECRET && typed !== storedValue) return false;
  }
  for (const field of ['base_url', 'endpoint']) {
    if (!Object.prototype.hasOwnProperty.call(current, field)) continue;
    if (normalizeUrl(current[field]) !== normalizeUrl(stored[field])) return true;
  }
  return false;
}

export function hasStoredSecret(schema, config) {
  const source = config || {};
  for (const field of secretFields(schema)) {
    const value = source[field];
    // A public profile carries the mask instead of the key, so the mask is
    // evidence of a stored secret, not an empty value.
    if (typeof value === 'string' && value.trim() !== '') return true;
  }
  return false;
}

// The form values for an edit: secrets are write-only, so a masked value
// leaves the input empty and the placeholder reports the stored key.
export function formValuesForEdit(schema, config) {
  const values = formValuesFromConfig(schema, config);
  for (const field of secretFields(schema)) {
    if (values[field] === MASKED_SECRET) values[field] = '';
  }
  return values;
}
