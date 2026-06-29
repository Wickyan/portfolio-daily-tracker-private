# DeepSeek Settings

Use the Settings page to configure DeepSeek without editing code.

## Open Settings

Open:

```text
http://服务器IP:55589/settings
```

Find the card:

```text
DeepSeekAPI配置
```

## Values

Use:

```text
API组: deepseek
BaseURL: https://api.deepseek.com/v1
Model: deepseek-chat
```

Paste your DeepSeek API Key into the APIKey password field.

## Security Notes

- `dashboard/config.json` stores the runtime API Key and must not be committed.
- The Settings API does not return the raw API Key to the frontend.
- After saving, refreshing Settings only shows a masked value such as `********abcd`.
- Do not paste API Keys into logs, screenshots, or committed files.

## Test Connection

Click:

```text
测试连接
```

The backend sends a minimal OpenAI-compatible `/chat/completions` request to DeepSeek.

## Save And Reload

Click:

```text
保存并重载
```

This writes the config to:

```text
dashboard/config.json
```

Then the backend reloads the LLM configuration without restarting the container.

## Verify Homepage AI

After saving, go back to the home page and send a short message to the AI chat.

If the response succeeds, the homepage AI is using the active LLM configuration.
