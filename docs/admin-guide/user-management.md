# User Management

The User Management tab is command central for controlling who has access to your Speakr instance and what they can do with it. Every user account flows through here, from initial creation to eventual removal, with all the monitoring and adjustments needed along the way. For system-wide usage patterns, check [system statistics](statistics.md).

![User Management](../assets/images/screenshots/Admin dashboard.png)

## Understanding the User Table

When you open User Management, you're greeted with a comprehensive table showing every user in your system. Each row tells a story - the email address identifies the user, the admin badge shows their privilege level, the recording count reveals their activity level, and the storage measurement demonstrates their resource consumption.

The search bar at the top responds instantly as you type, filtering the table to help you find specific users quickly. This becomes invaluable as your user base grows beyond what fits on a single screen. The table updates in real-time when you make changes, so you always see the current state of your system.

## Adding New Users

Creating a user account starts with clicking the Add User button in the top right corner. The modal that appears asks for the essentials - username, email address, and password. You'll also decide immediately whether this person needs admin privileges, though you can always change this later.

The username becomes their identity within Speakr, appearing in the interface and organizing their recordings. The email address serves dual purposes - it's their login credential and their contact point for any system communications. The password you set is temporary; users should change it immediately after their first login through their [account settings](../user-guide/settings.md). Configure initial [language preferences](../user-guide/settings.md#language-preferences) and [custom prompts](../user-guide/settings.md#custom-prompts-tab).

Admin privileges are powerful and should be granted sparingly. Admin users can see and modify all [system settings](system-settings.md), manage other users including other admins, configure [default prompts](prompts.md), and monitor the [vector store](vector-store.md). Most users will never need these capabilities.

## Managing Existing Users

Each user row includes action buttons that give you complete control over that account. The edit button opens a modal where you can update their username or email address. This is useful when people change names, switch email providers, or when you need to correct initial entry mistakes.

The admin toggle is perhaps the most powerful single click in the interface. Promoting a user to admin grants them access to everything you can see and do. Demoting an admin back to regular user immediately revokes all their administrative capabilities. The system prevents you from removing admin rights from your own account, ensuring you can't accidentally lock yourself out.

The delete button requires careful consideration. Removing a user is permanent and cannot be undone through the interface. All their recordings, notes, and settings will be deleted along with their account. The system asks for confirmation, but once confirmed, the removal is immediate and complete.

## Monitoring Usage Patterns

The recording count and storage columns reveal how users interact with your Speakr instance. High recording counts might indicate power users who rely heavily on the system, while low counts could suggest users who need training or might not need accounts at all.

Storage consumption tells another important story. Users with disproportionately high storage might be uploading very long recordings, keeping everything indefinitely, or possibly misusing the system. You can adjust [file size limits](system-settings.md#maximum-file-size) and review [chunking settings](../troubleshooting.md#files-over-25mb-fail-with-openai) if needed. This information helps you have informed conversations about resource usage and establish appropriate policies.

Patterns often emerge when you regularly review this data. You might notice seasonal variations in academic settings, project-based spikes in corporate environments, or gradual growth that signals the need for infrastructure expansion.

## Security Implications

Every user account is a potential security vector. Strong passwords are your first defense, but they're not enough alone. Encourage users to use unique passwords, change them regularly, and never share them with others.

Admin accounts require extra vigilance. Each admin can do everything you can do, including creating more admins or deleting all users. Limit admin access to the absolute minimum needed for operations. When someone no longer needs admin privileges, revoke them immediately.

Inactive accounts pose particular risks. They might have weak or compromised passwords that no one is monitoring. Regular audits help you identify and remove these vulnerabilities before they become problems.

---

Next: [System Statistics](statistics.md) →