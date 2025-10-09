# GPT-5 Support in Speakr

Speakr now supports OpenAI's GPT-5 model family with proper parameter handling according to OpenAI's Chat Completions API specifications.

## Requirements

- **OpenAI Python SDK**: Version 2.2.0 or higher (updated in `requirements.txt`)
- GPT-5 support was added to the OpenAI SDK in 2025

## Overview

GPT-5 models require different API parameters compared to previous models like GPT-4. When using GPT-5 models with the official OpenAI API, Speakr automatically detects this and adjusts the API calls accordingly.

## Supported GPT-5 Models

- `gpt-5` - Best for complex reasoning, broad world knowledge, and code-heavy tasks
- `gpt-5-mini` - Cost-optimized reasoning and chat; balances speed, cost, and capability
- `gpt-5-nano` - High-throughput tasks, especially simple instruction-following
- `gpt-5-chat-latest` - Latest GPT-5 chat model

## Key Differences from GPT-4

### Unsupported Parameters
GPT-5 models **do not support** the following parameters:
- `temperature` - Replaced by `reasoning_effort` and `verbosity`
- `top_p` - Not supported
- `logprobs` - Not supported

### New GPT-5 Parameters

#### Reasoning Effort
Controls how many reasoning tokens the model generates before producing a response.

- **minimal** - Fastest responses, minimal reasoning tokens (best for simple tasks)
- **low** - Fast responses with basic reasoning
- **medium** - Balanced reasoning and speed (default, recommended)
- **high** - Maximum reasoning for complex tasks like coding and multi-step planning

#### Verbosity
Controls how many output tokens are generated.

- **low** - Concise responses
- **medium** - Balanced detail (default)
- **high** - Thorough explanations and detailed code

#### Token Limit
- Uses `max_completion_tokens` instead of `max_tokens`

## Configuration

### Environment Variables

Add these settings to your `.env` file:

```bash
# Use OpenAI API endpoint
TEXT_MODEL_BASE_URL=https://api.openai.com/v1
TEXT_MODEL_API_KEY=your_openai_api_key
TEXT_MODEL_NAME=gpt-5-mini

# GPT-5 specific parameters (optional, defaults shown)
GPT5_REASONING_EFFORT=medium
GPT5_VERBOSITY=medium
```

### Example Configurations

#### Fast Summarization (Low Cost)
```bash
TEXT_MODEL_NAME=gpt-5-nano
GPT5_REASONING_EFFORT=minimal
GPT5_VERBOSITY=low
```

#### Standard Usage (Recommended)
```bash
TEXT_MODEL_NAME=gpt-5-mini
GPT5_REASONING_EFFORT=medium
GPT5_VERBOSITY=medium
```

#### Complex Analysis (High Quality)
```bash
TEXT_MODEL_NAME=gpt-5
GPT5_REASONING_EFFORT=high
GPT5_VERBOSITY=high
```

## Automatic Detection

Speakr automatically detects when you're using:
1. A GPT-5 model (based on model name)
2. The official OpenAI API (based on base URL containing `api.openai.com`)

When both conditions are met, it automatically:
- Removes `temperature` parameter from API calls
- Adds `reasoning_effort` parameter
- Adds `verbosity` parameter
- Uses `max_completion_tokens` instead of `max_tokens`
- Logs that GPT-5 parameters are being used

## Use Cases

### Summarization
- **Fast summaries**: `gpt-5-nano` with `minimal` effort and `low` verbosity
- **Standard summaries**: `gpt-5-mini` with `medium` effort and `medium` verbosity
- **Detailed summaries**: `gpt-5` with `medium` effort and `high` verbosity

### Chat
- **Quick Q&A**: `gpt-5-mini` with `minimal` effort and `low` verbosity
- **Standard conversation**: `gpt-5-mini` with `low` effort and `medium` verbosity
- **Complex analysis**: `gpt-5` with `high` effort and `medium` verbosity

## Troubleshooting

### Error: "Unsupported parameter 'temperature'"
This means GPT-5 detection failed. Check that:
1. `TEXT_MODEL_BASE_URL` contains `api.openai.com`
2. `TEXT_MODEL_NAME` starts with `gpt-5` or is one of: `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-chat-latest`

### Error: Invalid reasoning_effort value
Valid values are: `minimal`, `low`, `medium`, `high`

### Error: Invalid verbosity value
Valid values are: `low`, `medium`, `high`

## References

- [OpenAI GPT-5 Documentation](https://platform.openai.com/docs/guides/latest-model)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
