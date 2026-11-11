# Admin Guide

Welcome to the Speakr Admin Guide! As an administrator, you control the heart of your Speakr instance, managing users, monitoring system health, and configuring AI behavior.

## Administrative Controls

<div class="guide-cards">
  <div class="guide-card">
    <div class="card-icon">👥</div>
    <h3>User Management</h3>
    <p>Create accounts, manage permissions, monitor usage, and control access to your Speakr instance.</p>
    <a href="user-management" class="card-link">Manage Users →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">🤝</div>
    <h3>Group Management</h3>
    <p>Create groups, assign roles, configure auto-sharing tags, and enable organized collaboration.</p>
    <a href="group-management" class="card-link">Manage Groups →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">📊</div>
    <h3>System Statistics</h3>
    <p>Monitor system health, track usage patterns, and identify potential issues before they affect users.</p>
    <a href="statistics" class="card-link">View Statistics →</a>
  </div>
  
  <div class="guide-card">
    <div class="card-icon">🔧</div>
    <h3>System Settings</h3>
    <p>Configure global limits, timeouts, file sizes, and system-wide behavior that affects all users.</p>
    <a href="system-settings" class="card-link">Configure System →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">🤖</div>
    <h3>Model Configuration</h3>
    <p>Configure AI models for text generation, including GPT-5 support and provider selection.</p>
    <a href="model-configuration" class="card-link">Configure Models →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">✨</div>
    <h3>Default Prompts</h3>
    <p>Customize AI behavior with default summary prompts that shape how content is processed.</p>
    <a href="prompts" class="card-link">Set Prompts →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">🔍</div>
    <h3>Vector Store</h3>
    <p>Manage semantic search capabilities, monitor embedding status, and control Inquire Mode.</p>
    <a href="vector-store" class="card-link">Manage Search →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">🗑️</div>
    <h3>Retention & Auto-Deletion</h3>
    <p>Configure automated data lifecycle management with flexible retention policies and smart deletion rules.</p>
    <a href="retention" class="card-link">Manage Retention →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">📧</div>
    <h3>Email Setup</h3>
    <p>Configure email verification for new registrations and enable password reset functionality.</p>
    <a href="email-setup" class="card-link">Setup Email →</a>
  </div>

  <div class="guide-card">
    <div class="card-icon">🔐</div>
    <h3>SSO Setup</h3>
    <p>Integrate with identity providers like Keycloak, Azure AD, Google, or Auth0 using OpenID Connect.</p>
    <a href="sso-setup" class="card-link">Configure SSO →</a>
  </div>
</div>

## Quick Actions

<div class="action-cards">
  <div class="action-card">
    <span class="action-icon">➕</span>
    <div>
      <strong>Add New User</strong>
      <p>User Management → Add User Button → Enter details → Set permissions</p>
    </div>
  </div>

  <div class="action-card">
    <span class="action-icon">🤝</span>
    <div>
      <strong>Create a Group</strong>
      <p>Groups → Create group → Add members → Configure group tags</p>
    </div>
  </div>

  <div class="action-card">
    <span class="action-icon">📈</span>
    <div>
      <strong>Check System Health</strong>
      <p>System Statistics → Review metrics → Check processing status → Monitor storage</p>
    </div>
  </div>
  
  <div class="action-card">
    <span class="action-icon">⚙️</span>
    <div>
      <strong>Update Settings</strong>
      <p>System Settings → Adjust limits → Configure timeouts → Save changes</p>
    </div>
  </div>
  
  <div class="action-card">
    <span class="action-icon">🔄</span>
    <div>
      <strong>Process Embeddings</strong>
      <p>Vector Store → Check status → Process pending → Monitor progress</p>
    </div>
  </div>
</div>

## Need Admin Help?

<div class="help-section">
  <div class="help-item">
    <span class="help-icon">📖</span>
    <span>Review the detailed <a href="../troubleshooting.md">Troubleshooting Guide</a></span>
  </div>
  <div class="help-item">
    <span class="help-icon">🐛</span>
    <span>Check Docker logs: <code>docker compose logs -f app</code></span>
  </div>
  <div class="help-item">
    <span class="help-icon">💾</span>
    <span>Backup your data directory regularly</span>
  </div>
</div>

---

Ready to manage your Speakr instance? Start with [User Management](user-management.md) →