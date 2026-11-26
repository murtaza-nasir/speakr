import { describe, expect, it } from 'vitest';

import {
  buildConfigPayload,
  classifyFields,
  completeLabel,
  defaultLabel,
  displayValue,
  envConnectorName,
  fieldLabel,
  formValuesForEdit,
  formValuesFromConfig,
  hasStoredSecret,
  schemaControl,
  schemaDefaults,
  secretFields,
  urlChangeDropsSecret,
} from './asr-models.js';

const WHISPER = {
  type: 'object',
  required: ['api_key'],
  properties: {
    api_key: { type: 'string', writeOnly: true, description: 'OpenAI API key' },
    base_url: { type: 'string', description: 'API base URL' },
    model: { type: 'string', default: 'whisper-1' },
  },
};

const TENCENT = {
  type: 'object',
  required: ['secret_id', 'secret_key'],
  properties: {
    secret_id: { type: 'string' },
    secret_key: { type: 'string', writeOnly: true },
    model: { type: 'string', enum: ['16k_zh_en_meeting', '16k_zh'], default: '16k_zh_en_meeting' },
    hotword_id: { type: 'string' },
    timeout: { type: 'number', default: 1800 },
    diarize: { type: 'boolean', default: true },
    language_hints: { type: 'array', items: { type: 'string' }, default: [] },
    channel_id: { type: 'array', items: { type: 'integer' }, default: [] },
  },
};

const RICH = {
  type: 'object',
  properties: {
    model: { type: 'string', examples: ['large-v3', 'distil-large-v3'] },
    base_url: { type: 'string', default: 'http://asr:9000' },
    diarize: { type: 'boolean', default: true },
    timeout: { type: 'integer', default: 1800, minimum: 1 },
    language_hints: { type: 'array', items: { type: 'string' } },
    tags: { type: 'array', items: { type: 'object' } },
    metadata: { type: 'object' },
  },
};

describe('label autofill', () => {
  it('uses the lowercased environment connector name and model', () => {
    expect(envConnectorName('openai_whisper')).toBe('openai-whisper');
    expect(envConnectorName('asr_endpoint')).toBe('whisper-asr');
    expect(envConnectorName('Tencent_ASR')).toBe('tencent-asr');
    expect(defaultLabel('openai_whisper', 'Whisper-1')).toBe('openai-whisper:whisper-1');
    expect(defaultLabel('asr_endpoint', 'small')).toBe('whisper-asr:small');
  });

  it('leaves the model empty rather than inventing one', () => {
    expect(defaultLabel('tencent_asr', null)).toBe('tencent-asr:');
    expect(defaultLabel('', 'small')).toBe(':small');
  });

  it('fills a label that ends with a colon when a model is chosen', () => {
    expect(completeLabel('whisper-asr:', 'small')).toBe('whisper-asr:small');
    expect(completeLabel('whisper-asr:', 'Large-V3')).toBe('whisper-asr:large-v3');
    expect(completeLabel('  my provider: ', 'Small ')).toBe('my provider:small');
  });

  it('keeps a label the user wrote past the colon', () => {
    expect(completeLabel('whisper-asr:large-v3', 'small')).toBe('whisper-asr:large-v3');
    expect(completeLabel('Whisper ASR', 'small')).toBe('Whisper ASR');
  });

  it('drops a dangling colon when there is no model', () => {
    expect(completeLabel('whisper-asr:', '')).toBe('whisper-asr');
    expect(completeLabel('whisper-asr:', null)).toBe('whisper-asr');
    expect(completeLabel('', '')).toBe('');
  });
});

describe('schema mapping', () => {
  it('splits common, model and advanced fields in schema order', () => {
    const fields = classifyFields(TENCENT);
    expect(fields.common.map(([key]) => key)).toEqual(['secret_id', 'secret_key']);
    expect(fields.model[0]).toBe('model');
    expect(fields.advanced.map(([key]) => key)).toEqual([
      'hotword_id', 'timeout', 'diarize', 'language_hints', 'channel_id',
    ]);
    expect(secretFields(TENCENT)).toEqual(['secret_key']);
  });

  it('maps each property to its control', () => {
    expect(schemaControl({ type: 'boolean' })).toBe('boolean');
    expect(schemaControl({ type: 'integer' })).toBe('number');
    expect(schemaControl({ type: 'number' })).toBe('number');
    expect(schemaControl({ type: 'string', enum: ['a'] })).toBe('enum');
    expect(schemaControl({ type: 'string', writeOnly: true })).toBe('secret');
    expect(schemaControl({ type: 'string', examples: ['large-v3'] })).toBe('text');
    expect(schemaControl({ type: 'array', items: { type: 'string' } })).toBe('list');
    expect(schemaControl({ type: 'array', items: { type: 'integer' } })).toBe('list');
    expect(schemaControl({ type: 'array', items: { type: 'object' } })).toBe('json');
    expect(schemaControl({ type: 'object' })).toBe('json');
  });

  it('labels known fields and title-cases unknown ones', () => {
    expect(fieldLabel('base_url')).toBe('Base URL');
    expect(fieldLabel('return_speaker_embeddings')).toBe('Return speaker embeddings');
    expect(fieldLabel('custom_option')).toBe('Custom Option');
  });

  it('reads defaults without sharing arrays', () => {
    const defaults = schemaDefaults(TENCENT);
    expect(defaults).toMatchObject({
      model: '16k_zh_en_meeting', timeout: 1800, diarize: true,
      language_hints: [], channel_id: [],
    });
    defaults.channel_id.push(1);
    expect(schemaDefaults(TENCENT).channel_id).toEqual([]);
  });

  it('turns a stored config into control values', () => {
    const values = formValuesFromConfig(TENCENT, {
      secret_id: 'id', secret_key: 'secret', diarize: false,
      language_hints: ['en', 'es'], channel_id: [1, 2],
    });
    expect(values.secret_key).toBe('secret');
    expect(values.diarize).toBe(false);
    expect(values.language_hints).toBe('en, es');
    expect(values.channel_id).toBe('1, 2');
    expect(values.timeout).toBe('');
    expect(displayValue({ type: 'object' }, { a: 1 })).toBe('{"a":1}');
  });

  it('leaves a masked secret out of the edit prefill', () => {
    const values = formValuesForEdit(TENCENT, { secret_key: '***', secret_id: 'id' });
    expect(values.secret_key).toBe('');
    const environment = formValuesForEdit(TENCENT, { secret_key: 'real', secret_id: 'id' });
    expect(environment.secret_key).toBe('real');
  });
});

describe('payload builder', () => {
  it('sends a typed secret and keeps booleans and numbers', () => {
    const payload = buildConfigPayload(TENCENT, {
      secret_id: 'id', secret_key: 'new-secret', model: '16k_zh',
      hotword_id: '', timeout: '900', diarize: false,
      language_hints: 'en, es', channel_id: '1, 2',
    });
    expect(payload.clearSecrets).toBe(false);
    expect(payload.config).toEqual({
      secret_id: 'id', secret_key: 'new-secret', model: '16k_zh',
      hotword_id: '', timeout: 900, diarize: false,
      language_hints: ['en', 'es'], channel_id: [1, 2],
    });
  });

  it('keeps the stored secret when a secret field is empty', () => {
    const payload = buildConfigPayload(WHISPER, { api_key: '', base_url: 'https://api' });
    expect(payload.clearSecrets).toBe(false);
    expect(payload.config).toEqual({ base_url: 'https://api', model: '' });
  });

  it('honours the clear checkbox even when the field shows a stored key', () => {
    const payload = buildConfigPayload(WHISPER, { api_key: 'stored', model: '' }, { clearSecrets: true });
    expect(payload.clearSecrets).toBe(true);
    expect(payload.config.api_key).toBe('stored');
  });

  it('omits an empty secret from a test payload and never clears', () => {
    const payload = buildConfigPayload(WHISPER, { api_key: '', model: 'whisper-1' });
    expect(payload.clearSecrets).toBe(false);
    expect(payload.config).toEqual({ base_url: '', model: 'whisper-1' });
  });

  it('treats the mask as not a new secret', () => {
    const payload = buildConfigPayload(WHISPER, { api_key: '***' });
    expect(payload.clearSecrets).toBe(false);
    expect(payload.config.api_key).toBeUndefined();
  });

  it('parses JSON textareas and reports invalid JSON', () => {
    const payload = buildConfigPayload(RICH, { tags: '[]', metadata: '{"a": 1}' });
    expect(payload.config.tags).toEqual([]);
    expect(payload.config.metadata).toEqual({ a: 1 });
    expect(() => buildConfigPayload(RICH, { tags: '{' })).toThrow(/JSON/);
    expect(() => buildConfigPayload(RICH, { metadata: '[]' })).toThrow(/JSON object/);
  });

  it('rejects an integer field holding a fraction', () => {
    expect(() => buildConfigPayload(RICH, { timeout: '1.5' })).toThrow(/integer/);
  });
});

describe('URL change warning', () => {
  const stored = { api_key: 'old-key', base_url: 'https://old.example/v1' };

  it('warns when the URL changed and the stored key stayed', () => {
    expect(urlChangeDropsSecret(WHISPER, stored,
      { api_key: 'old-key', base_url: 'https://new.example/v1' })).toBe(true);
  });

  it('does not warn when a new key is typed', () => {
    expect(urlChangeDropsSecret(WHISPER, stored,
      { api_key: 'new-key', base_url: 'https://new.example/v1' })).toBe(false);
  });

  it('ignores a trailing slash and a connector without secrets', () => {
    expect(urlChangeDropsSecret(WHISPER, stored,
      { api_key: 'old-key', base_url: 'https://old.example/v1/' })).toBe(false);
    expect(urlChangeDropsSecret({ properties: { base_url: { type: 'string' } } }, stored,
      { base_url: 'https://new.example/v1' })).toBe(false);
  });

  it('detects a stored secret from a value or the public mask', () => {
    expect(hasStoredSecret(WHISPER, { api_key: 'old-key' })).toBe(true);
    expect(hasStoredSecret(WHISPER, { api_key: '' })).toBe(false);
    expect(hasStoredSecret(WHISPER, { api_key: '***' })).toBe(true);
    expect(hasStoredSecret({ properties: { base_url: { type: 'string' } } }, { base_url: 'x' })).toBe(false);
  });
});
