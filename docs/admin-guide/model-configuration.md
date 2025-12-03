# Model Configuration

This guide covers how to configure AI models for text generation in Speakr, including support for OpenAI's GPT-5 series and other language models.

## Overview

Speakr uses AI models for several key features:

- **Summary Generation**: Creating intelligent summaries of your transcriptions
- **Title Generation**: Automatically generating descriptive titles for recordings
- **Event Extraction**: Identifying calendar-worthy events from conversations
- **Interactive Chat**: Answering questions about your recordings
- **Speaker Identification**: Detecting speaker names from conversation context

These features are powered by large language models (LLMs) configured through your `.env` file.

## Basic Configuration

The text generation model is configured using three environment variables:

```bash
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=your_api_key_here
TEXT_MODEL_NAME=openai/gpt-4o-mini
```

### Choosing a Provider

**OpenRouter** (recommended for most users): Provides access to multiple AI models through a single API, often at competitive prices. Supports GPT-4, Claude, and many other models. Configure using `TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1`.

**OpenAI Direct**: Use OpenAI's API directly for access to their latest models including GPT-5. Configure using `TEXT_MODEL_BASE_URL=https://api.openai.com/v1`. This option is required for GPT-5 models with their specialized parameters.

**Custom Endpoints**: Speakr works with any OpenAI-compatible API endpoint, including self-hosted solutions like LocalAI, Ollama with OpenAI compatibility, or enterprise API gateways.

## GPT-5 Support

Speakr fully supports OpenAI's GPT-5 model family, automatically detecting and adjusting API parameters when you use GPT-5 models with the official OpenAI API.

### Requirements

- **OpenAI Python SDK**: Version 2.2.0 or higher (included in `requirements.txt`)
- **OpenAI API**: Must use `TEXT_MODEL_BASE_URL=https://api.openai.com/v1`
- **Valid API Key**: An OpenAI API key with GPT-5 access

### Supported GPT-5 Models

- **gpt-5**: Best for complex reasoning, broad world knowledge, and code-heavy tasks
- **gpt-5-mini**: Cost-optimized reasoning and chat; balances speed, cost, and capability
- **gpt-5-nano**: High-throughput tasks, especially simple instruction-following
- **gpt-5-chat-latest**: Latest GPT-5 chat model

### Key Differences from GPT-4

GPT-5 models use different parameters than previous models:

**Unsupported Parameters** (will cause errors if used):
- `temperature` - Replaced by `reasoning_effort` and `verbosity`
- `top_p` - Not supported
- `logprobs` - Not supported

**New GPT-5 Parameters**:

**Reasoning Effort**: Controls how many reasoning tokens the model generates before producing a response.

- **minimal**: Fastest responses, minimal reasoning tokens (best for simple tasks)
- **low**: Fast responses with basic reasoning
- **medium**: Balanced reasoning and speed (default, recommended)
- **high**: Maximum reasoning for complex tasks like coding and multi-step planning

**Verbosity**: Controls how many output tokens are generated.

- **low**: Concise responses
- **medium**: Balanced detail (default)
- **high**: Thorough explanations and detailed code

**Token Limits**: GPT-5 uses `max_completion_tokens` instead of `max_tokens`.

### Configuring GPT-5

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

### GPT-5 Configuration Examples

**Fast Summarization (Low Cost)**:
```bash
TEXT_MODEL_NAME=gpt-5-nano
GPT5_REASONING_EFFORT=minimal
GPT5_VERBOSITY=low
```

**Standard Usage (Recommended)**:
```bash
TEXT_MODEL_NAME=gpt-5-mini
GPT5_REASONING_EFFORT=medium
GPT5_VERBOSITY=medium
```

**Complex Analysis (High Quality)**:
```bash
TEXT_MODEL_NAME=gpt-5
GPT5_REASONING_EFFORT=high
GPT5_VERBOSITY=high
```

### Automatic Detection

Speakr automatically detects when you're using:

1. A GPT-5 model (based on model name)
2. The official OpenAI API (based on base URL containing `api.openai.com`)

When both conditions are met, Speakr automatically:

- Removes `temperature` parameter from API calls
- Adds `reasoning_effort` parameter
- Adds `verbosity` parameter
- Uses `max_completion_tokens` instead of `max_tokens`
- Logs that GPT-5 parameters are being used

Check your logs for confirmation:
```
Using GPT-5 model: gpt-5-mini - applying GPT-5 specific parameters
```

### Using GPT-5 Through OpenRouter

If you use GPT-5 models through OpenRouter or other proxy services, the automatic GPT-5 parameter handling will **not** activate. These services typically handle parameter translation themselves, so Speakr uses standard parameters (temperature, max_tokens, etc.).

### Use Cases

**Summarization**:
- Fast summaries: `gpt-5-nano` with `minimal` effort and `low` verbosity
- Standard summaries: `gpt-5-mini` with `medium` effort and `medium` verbosity
- Detailed summaries: `gpt-5` with `medium` effort and `high` verbosity

**Chat**:
- Quick Q&A: `gpt-5-mini` with `minimal` effort and `low` verbosity
- Standard conversation: `gpt-5-mini` with `low` effort and `medium` verbosity
- Complex analysis: `gpt-5` with `high` effort and `medium` verbosity

### Troubleshooting GPT-5

**Error: "Unsupported parameter 'temperature'"**

This means GPT-5 detection failed. Check that:

1. `TEXT_MODEL_BASE_URL` contains `api.openai.com`
2. `TEXT_MODEL_NAME` starts with `gpt-5` or is one of: `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-chat-latest`

**Error: "Invalid reasoning_effort value"**

Valid values are: `minimal`, `low`, `medium`, `high`

**Error: "Invalid verbosity value"**

Valid values are: `low`, `medium`, `high`

### Migrating from GPT-4 to GPT-5

1. **Update dependencies** (required for GPT-5):
   ```bash
   pip install -r requirements.txt
   ```
   This upgrades the OpenAI SDK to version 2.2.0 or higher.

2. Update your `.env` file:
   ```bash
   TEXT_MODEL_NAME=gpt-5-mini  # or gpt-5, gpt-5-nano
   ```

3. Add GPT-5 parameters (optional):
   ```bash
   GPT5_REASONING_EFFORT=medium
   GPT5_VERBOSITY=medium
   ```

4. Restart Speakr:
   ```bash
   docker compose restart
   ```

5. Check logs for confirmation:
   ```
   Using GPT-5 model: gpt-5-mini - applying GPT-5 specific parameters
   ```

### Performance Considerations

- **Cost**: `gpt-5-nano` < `gpt-5-mini` < `gpt-5`
- **Speed**: `minimal` < `low` < `medium` < `high` reasoning effort
- **Quality**: Generally increases with model size and reasoning effort
- **Token usage**: Higher verbosity = more output tokens

For most use cases, we recommend:
- **Model**: `gpt-5-mini`
- **Reasoning**: `medium`
- **Verbosity**: `medium`

This provides a good balance of cost, speed, and quality.

## Separate Chat Model Configuration

Speakr allows you to configure a separate model specifically for real-time chat interactions, while using a different model for background tasks like summarization and title generation. This enables you to:

- **Use different service tiers**: Configure a faster, more expensive model for interactive chat while using a cheaper model for background processing
- **Optimize costs**: Use a budget-friendly model for summarization while keeping a high-quality model for chat
- **Balance speed and quality**: Prioritize low latency for chat while accepting slower processing for summaries

### Configuration

Add these optional environment variables to your `.env` file:

```bash
# Chat Model Configuration (Optional)
# If not set, chat will use TEXT_MODEL_* settings
CHAT_MODEL_API_KEY=your_chat_api_key
CHAT_MODEL_BASE_URL=https://openrouter.ai/api/v1
CHAT_MODEL_NAME=openai/gpt-4o
```

### Fallback Behavior

| Configuration | Behavior |
|--------------|----------|
| No `CHAT_MODEL_*` variables set | Chat uses `TEXT_MODEL_*` settings (default) |
| Only `CHAT_MODEL_NAME` set | Falls back to `TEXT_MODEL_*` (API key required) |
| Only `CHAT_MODEL_API_KEY` set | Falls back to `TEXT_MODEL_*` (model name required) |
| `CHAT_MODEL_API_KEY` + `CHAT_MODEL_NAME` set | Uses chat config with `TEXT_MODEL_BASE_URL` |
| All `CHAT_MODEL_*` variables set | Uses fully dedicated chat configuration |

### GPT-5 Settings for Chat

If you use GPT-5 models for chat, you can configure separate GPT-5 parameters:

```bash
# Chat-specific GPT-5 settings (optional)
# Falls back to GPT5_* settings if not specified
CHAT_GPT5_REASONING_EFFORT=medium
CHAT_GPT5_VERBOSITY=medium
```

### Example Configurations

**Cheap Summarization + Premium Chat**:
```bash
# Background tasks: Use budget model
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=your_openrouter_key
TEXT_MODEL_NAME=openai/gpt-4o-mini

# Interactive chat: Use premium model
CHAT_MODEL_API_KEY=your_openai_key
CHAT_MODEL_BASE_URL=https://api.openai.com/v1
CHAT_MODEL_NAME=gpt-5-mini
CHAT_GPT5_REASONING_EFFORT=low
CHAT_GPT5_VERBOSITY=medium
```

**Same Provider, Different Models**:
```bash
# Background tasks: Smaller model
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=your_api_key
TEXT_MODEL_NAME=google/gemini-2.5-flash-lite

# Interactive chat: Larger model (same provider)
CHAT_MODEL_NAME=openai/gpt-4o
# Note: CHAT_MODEL_API_KEY not needed if using same key
# Note: CHAT_MODEL_BASE_URL not needed if using same endpoint
```

**Different Service Tiers (OpenAI)**:
```bash
# Background tasks: Standard tier
TEXT_MODEL_BASE_URL=https://api.openai.com/v1
TEXT_MODEL_API_KEY=your_standard_tier_key
TEXT_MODEL_NAME=gpt-4o-mini

# Interactive chat: Priority tier for faster responses
CHAT_MODEL_API_KEY=your_priority_tier_key
CHAT_MODEL_NAME=gpt-4o
```

### When to Use Separate Chat Models

**Recommended for**:
- High-volume deployments where chat responsiveness is critical
- Users who need different service tiers for different operations
- Cost optimization when chat usage is significantly higher than summarization

**Not needed for**:
- Small deployments with low usage
- When using the same model for all operations is acceptable
- Simple setups where configuration simplicity is preferred

## Model Selection Guidelines

### For Summaries

The model you choose significantly impacts summary quality:

- **GPT-4 or better**: Produces nuanced, context-aware summaries with excellent understanding of complex topics
- **GPT-5-mini**: Excellent balance of quality and cost for most summarization needs
- **GPT-3.5/4o-mini**: Budget-friendly option, suitable for straightforward content
- **Claude models**: Strong performance on structured content and technical material

### For Chat

Chat features benefit from more capable models:

- **GPT-5**: Best for complex multi-turn conversations and detailed analysis
- **GPT-5-mini**: Recommended for most chat use cases
- **Claude**: Excellent for technical discussions and code-related queries

### Cost Optimization

To reduce costs while maintaining quality:

1. **Use smaller models for simple tasks**: `gpt-5-nano` or `gpt-4o-mini` handle straightforward summaries well
2. **Adjust GPT-5 reasoning effort**: Use `minimal` or `low` for quick tasks
3. **Set token limits**: Configure `SUMMARY_MAX_TOKENS` and `CHAT_MAX_TOKENS` in your `.env`
4. **Use OpenRouter**: Often provides better rates than direct API access

### Testing Configuration

After changing model configuration:

1. Restart the Speakr container
2. Create a test recording
3. Review the generated summary and title
4. Test the chat feature
5. Monitor logs for any errors or warnings

## Environment Variables Reference

```bash
# Required: API endpoint
TEXT_MODEL_BASE_URL=https://api.openai.com/v1

# Required: API key
TEXT_MODEL_API_KEY=your_api_key_here

# Required: Model identifier
TEXT_MODEL_NAME=gpt-5-mini

# Optional: Maximum tokens for summaries (default: 8000)
SUMMARY_MAX_TOKENS=8000

# Optional: Maximum tokens for chat responses (default: 2000)
CHAT_MAX_TOKENS=2000

# GPT-5 specific (only used with GPT-5 models and OpenAI API)
GPT5_REASONING_EFFORT=medium  # minimal, low, medium, high
GPT5_VERBOSITY=medium          # low, medium, high

# Chat model configuration (optional - falls back to TEXT_MODEL_* if not set)
CHAT_MODEL_API_KEY=your_chat_api_key
CHAT_MODEL_BASE_URL=https://openrouter.ai/api/v1
CHAT_MODEL_NAME=openai/gpt-4o

# Chat-specific GPT-5 settings (optional - falls back to GPT5_* if not set)
CHAT_GPT5_REASONING_EFFORT=medium  # minimal, low, medium, high
CHAT_GPT5_VERBOSITY=medium          # low, medium, high
```

## Troubleshooting

### Model Not Responding

Check logs for authentication errors:
```bash
docker compose logs -f app | grep "LLM"
```

Common issues:
- Invalid API key
- Model name not available on your plan
- Rate limits exceeded
- Insufficient credits

### Poor Summary Quality

Try these adjustments:
- Upgrade to a more capable model
- Increase `SUMMARY_MAX_TOKENS`
- Review and refine [custom prompts](prompts.md)
- For GPT-5: increase reasoning effort to `medium` or `high`

### High Costs

Reduce costs with:
- Switch to smaller models (`gpt-5-nano`, `gpt-4o-mini`)
- Lower token limits
- For GPT-5: reduce reasoning effort to `minimal` or `low`
- Use OpenRouter for better rates

## Additional Resources

- [OpenAI GPT-5 Documentation](https://platform.openai.com/docs/guides/latest-model)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
- [OpenRouter Documentation](https://openrouter.ai/docs)
- [Custom Prompts Guide](prompts.md)
- [System Settings](system-settings.md)

---

Next: [Default Prompts](prompts.md) | Back to [Admin Guide](index.md)
