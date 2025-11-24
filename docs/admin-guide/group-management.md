---
layout: default
title: Group Management
parent: Admin Guide
nav_order: 6
---

# Group Management

Groups enable organized collaboration in multi-user Speakr instances by grouping users and automating recording access through group-specific tags. This powerful feature reduces administrative overhead while maintaining security and control over content access.

## Prerequisites

Before enabling groups, ensure internal sharing is configured. Groups build on Speakr's internal sharing infrastructure to automatically grant access when users apply group tags.

### Required Configuration

Add these settings to your `.env` file:

```bash
# Enable internal sharing (required for groups)
ENABLE_INTERNAL_SHARING=true

# Control username visibility
SHOW_USERNAMES_IN_UI=true   # Show usernames in UI
# OR
SHOW_USERNAMES_IN_UI=false  # Hide usernames (users type usernames manually to share)
```

After modifying `.env`, restart your Speakr instance for changes to take effect. The Groups tab will appear in the admin dashboard once internal sharing is enabled.

### Privacy Considerations

The `SHOW_USERNAMES_IN_UI` setting affects the entire instance. When enabled (`true`), users see actual usernames when searching for colleagues and viewing shared content. This improves usability in small, trusted groups where everyone knows each other.

When disabled (`false`), usernames are hidden from the interface. Users must know each other's usernames to share recordings - they type the username manually when creating shares. This privacy-focused approach suits organizations where username visibility should be restricted. Group functionality works identically in both modes - only the display changes.

## Creating and Managing Groups

Groups are created and managed exclusively through the admin dashboard. Regular users cannot create groups, ensuring centralized control over organizational structure.

### Creating a Group

Navigate to the Admin Dashboard and select the Groups tab. Click "Create Group" to open the creation modal. Provide a group name that clearly identifies the group's purpose - "Engineering", "Sales EMEA", "Project Phoenix", etc. Descriptive names help users understand each group's scope and purpose.

The description field is optional but recommended. Use it to explain the group's purpose, which projects or departments it serves, or who should be members. Good descriptions help future administrators understand group organization and make membership decisions.

Click "Create Group" to finalize creation. The group appears in your groups list immediately, though it starts with no members. The creating user (you) doesn't automatically join - membership must be explicitly granted even for group creators.

### Managing Group Membership

Click the users-cog icon next to any group to open the group management modal. This interface shows current members, their roles, and provides tools for adding or removing members.

To add a member, select a user from the dropdown and choose their role:

- **Member**: Can use group tags and access group-tagged recordings. Suitable for most group participants who need to collaborate on content.
- **Admin**: All member capabilities plus the ability to manage group membership, create and delete group tags, and access group management features. Useful for group leads or managers who need administrative control.

Click "Add Member" to grant access. The user immediately gains visibility to group tags and will receive future group-tagged recordings. They don't automatically gain access to existing group-tagged recordings - only new ones tagged after they join.

### Changing Member Roles

Group roles can change as responsibilities evolve. Click the role dropdown next to any member to toggle them between Member and Admin. Role changes take effect immediately.

Promoting members to admin grants them access to group management capabilities. They'll see a "Group Management" link in their interface and can add/remove members, create tags, and manage the group independently. This distributes administrative workload and empowers group leads.

Demoting admins to members removes their management capabilities but preserves their group membership. They retain access to group tags and recordings but can no longer manage group membership or tags.

### Removing Group Members

Click the red user-times icon next to any member to remove them from the group. Removal is immediate and has several effects:

- The user loses access to group tags
- They won't receive new group-tagged recordings
- Their access to previously-shared group recordings persists
- Their personal notes on group recordings are preserved

If you need to fully revoke access to existing group recordings, you must manually revoke those internal shares through the recording's share management interface. Group removal only prevents future automatic sharing.

### Deleting Groups

Click the red trash icon next to a group to delete it entirely. A confirmation dialog prevents accidental deletion. Deleting a group:

- Removes all group memberships
- Deletes all group tags (via database cascade)
- Preserves all recordings (including previously group-tagged ones)
- Preserves all internal shares created by group tags

Group deletion is irreversible. Once deleted, the group structure is gone, though recordings and access permissions created by the group persist. If you need to temporarily disable a group, consider removing all members instead of deleting the group itself.

## Group Tags

Group tags power automatic sharing within groups. Unlike personal tags that organize individual content, group tags trigger access grants across all group members whenever applied.

### Creating Group Tags

From the Groups tab, click the purple tags icon next to the relevant group. This opens the group tags modal showing existing tags and a creation form.

Provide a tag name that describes the content type or purpose. Good names are specific and clear: "Sprint Reviews", "Customer Calls", "Legal Contracts". Avoid generic names like "Important" or "Group" that don't convey useful information.

Select a color to visually distinguish the tag. Colors help users quickly identify content categories in the interface. Consider establishing color conventions - blue for technical content, green for sales, red for legal, etc.

### Tag Retention Policies

Group tags can override global retention settings with tag-specific retention periods. This powerful feature lets different content types have different lifecycles within the same instance.

Leave the retention field empty to use global retention settings. The tag won't affect how long recordings are kept - they'll follow the instance-wide `GLOBAL_RETENTION_DAYS` setting.

Enter a number of days to set custom retention for this tag. Recordings with this tag will be auto-deleted after the specified period, regardless of global settings. For example:

- Legal group: 2555 days (7 years) for contracts and compliance recordings
- Operations group: 14 days for daily stand-ups
- Marketing group: 180 days for campaign planning sessions

When a recording has multiple tags with different retention periods, the shortest period applies. This ensures content is never kept longer than its most restrictive tag allows.

### Protection from Deletion

Enable "Protect from deletion" to make recordings with this tag immune to automatic deletion. Protected recordings are never auto-deleted regardless of age, global retention settings, or other tag retention periods.

Use protection for recordings that must be permanently preserved:

- Legal and compliance records
- Critical business decisions
- Training and onboarding materials
- Reference documentation
- Historical archives

Protection can be removed by editing the tag later if preservation requirements change. Removing protection doesn't immediately delete recordings - they'll be evaluated for deletion on the next retention check based on their age and other applicable retention policies.

### Auto-Share Settings

Group tags support two levels of automatic sharing that trigger when any group member applies the tag to a recording:

**Share with All Group Members** is the default and recommended approach. When enabled, applying this tag shares the recording with every group member (excluding the owner). All members receive view and edit permissions, enabling full collaboration.

**Share with Group Leads Only** restricts automatic sharing to group admins. When enabled, only users with the admin role in this group receive automatic access. Regular members don't get automatic access, though group admins can manually share with them if needed. This option suits sensitive content that requires administrative oversight before wider distribution.

Both options can be enabled simultaneously, though this is redundant - sharing with all members already includes group leads. Use one or the other based on your content sensitivity and group structure.

### Managing Group Tags

Existing group tags appear in the group tags modal with their current settings. Click the edit icon to modify a tag's name, color, retention, protection, or sharing settings. Changes affect the tag going forward but don't retroactively change already-applied tags or shares.

Delete group tags by clicking the trash icon. Deleted tags:

- Are removed from all recordings they were applied to
- Disappear from tag selectors for all group members
- Don't delete the recordings themselves
- Don't revoke access already granted through the tag

If a tag was widely used, consider the impact before deletion. Users may have organized content around that tag, and deletion removes that organizational structure. In most cases, retaining unused tags causes no harm.

### Syncing Group Shares

If your instance enabled group features after recordings were already tagged, or if group membership changed significantly, you might have group-tagged recordings that weren't automatically shared with current group members. The "Sync Group Shares" feature addresses this.

Click "Sync Group Shares" in the group management modal to open the sync dialog. Review the information about what the sync will do - it applies automatic sharing retroactively to all existing recordings with this group's tags.

The sync operation:

- Identifies all recordings tagged with any of this group's tags
- Checks each recording for existing shares with current group members
- Creates missing shares for group members who should have access but don't
- Respects the tag's sharing settings (all members vs. group leads only)
- Skips recordings where members already have access

Confirm the sync to execute. Depending on the number of tagged recordings and group size, this might take a few seconds to several minutes. A result modal shows how many shares were created and how many recordings were processed.

Sync is safe to run multiple times - it won't create duplicate shares. Use it after adding many new members, after fixing misconfigured tags, or when migrating from older Speakr versions that didn't have full group support.

## Group Admin Role

Group admins are group members with elevated permissions within their group's scope. Unlike full instance administrators who can manage all groups and system settings, group admins can only manage groups where they have the admin role.

### Granting Group Admin Access

When adding a member to a group, select "Admin" from the role dropdown. The user immediately gains group admin capabilities for that group only. They cannot manage other groups or access system-wide administrative features.

Group admins see a "Group Management" link in their user menu instead of the full admin link. Clicking this takes them to a focused admin interface showing only groups they administer. The interface is identical to the Groups tab regular admins see, but scoped to their groups.

### Group Admin Capabilities

Group admins can perform these actions within their groups:

- Add new members from the instance's user base
- Remove existing members (excluding themselves)
- Change member roles between admin and member
- Create new group tags with full configuration options
- Edit existing group tags including retention and sharing settings
- Delete group tags
- Sync group shares for their groups

Group admins cannot:

- Create new groups
- Delete groups
- Manage groups they're not admins of
- Access system-wide admin features (users, settings, statistics)
- Grant themselves admin access to other groups

This scoped access lets you distribute group management responsibility to group leads without granting full administrative access. Group leads can manage their groups independently while you maintain control over instance-wide settings and group creation.

### Security Boundaries

Group admins have powerful capabilities within their groups but cannot escalate their privileges. They cannot:

- Make themselves full instance administrators
- Grant themselves admin roles in other groups
- Access or modify system settings
- View statistics for other groups or the entire instance
- Delete recordings owned by other users (even within their group)

The database enforces these boundaries at the API level. Even if a group admin could somehow call instance-wide admin APIs, the backend verifies permissions and rejects unauthorized requests. The UI simply hides controls group admins can't use, but security doesn't rely on UI hiding.

## Configuration Reference

### Environment Variables

```bash
# Internal Sharing (Required)
ENABLE_INTERNAL_SHARING=true|false

# Username Display
SHOW_USERNAMES_IN_UI=true|false

# Public Sharing Control (Affects group members' public sharing)
ENABLE_PUBLIC_SHARING=true|false

# Retention Settings (Groups can override)
ENABLE_AUTO_DELETION=true|false
GLOBAL_RETENTION_DAYS=90
DELETION_MODE=audio_only|full_recording
```

### Database Schema

Groups use several database tables that work together:

**Group Table**:
- `id`: Primary key
- `name`: Group name (max 100 chars)
- `description`: Optional group description
- `created_by`: User ID of creator (full admin)
- `created_at`: Creation timestamp

**TeamMembership Table**:
- `id`: Primary key
- `team_id`: References Group
- `user_id`: References User
- `role`: "admin" or "member"
- `joined_at`: Membership timestamp

**Tag Table** (Extended):
- `team_id`: References Group (null for personal tags)
- `retention_days`: Custom retention override (null uses global)
- `protect_from_deletion`: Boolean protection flag
- `auto_share_on_apply`: Boolean (share with all members)
- `share_with_team_lead`: Boolean (share with group admins only)

Cascade deletion is configured so deleting a group deletes its tags and memberships, but preserves recordings and shares.

## Troubleshooting

### Groups Tab Not Visible

**Cause**: Internal sharing not enabled or not configured correctly.

**Solution**:
1. Check `.env` contains `ENABLE_INTERNAL_SHARING=true`
2. Restart Speakr after `.env` changes
3. Clear browser cache and reload
4. Check application logs for startup errors

### Users Can't See Group Tags

**Cause**: User not added to group, or internal sharing disabled.

**Solution**:
1. Verify user is listed in group membership
2. Confirm `ENABLE_INTERNAL_SHARING=true` in `.env`
3. Check user is logged in (group tags hidden for anonymous users)
4. Refresh the page to load updated tag lists

### Auto-Sharing Not Working

**Cause**: Group tag misconfigured or internal sharing disabled.

**Solution**:
1. Edit the group tag and verify "Share with all group members" or "Share with group leads" is enabled
2. Confirm `ENABLE_INTERNAL_SHARING=true` in `.env`
3. Check application logs when applying tags for sharing errors
4. Try manually sharing the recording to verify sharing infrastructure works

### Group Admin Can't Access Admin Interface

**Cause**: User doesn't have admin role in any group, or routing issue.

**Solution**:
1. Verify user role is "admin" not "member" in group membership
2. Have user log out and back in to refresh session
3. Check "Group Management" link appears in user menu (not "Admin")
4. Review application logs for permission errors

### Recordings Not Deleted Per Retention Policy

**Cause**: Protected tags, misconfigured retention, or auto-deletion disabled.

**Solution**:
1. Check if recording has protected group tags
2. Verify `ENABLE_AUTO_DELETION=true` in `.env`
3. Confirm `GLOBAL_RETENTION_DAYS` is set if no tag retention applies
4. Review cron scheduler logs for deletion errors
5. Check tag retention_days is set correctly (null = use global)

### Sync Group Shares Shows Zero Shares Created

**Cause**: All applicable shares already exist, or no recordings have group tags.

**Solution**:
1. Verify recordings actually have tags from this group
2. Check if group members already have access via other shares
3. Review whether recordings are owned by current group members (no self-sharing)
4. Confirm group has members beyond the recording owners

## Best Practices

### Group Structure

**Small Organizations (<10 users)**:
Create groups per department (Engineering, Sales, HR). Use group tags for project names or content types. Liberal use of groups promotes collaboration since everyone knows everyone.

**Large Organizations (>10 users)**:
Create groups per product, division, or major project. Use nested organizational patterns if needed (separate groups for Product A Engineering and Product A Sales). More selective group membership prevents information overload.

### Tag Naming Conventions

Establish conventions early and document them for consistency:

```
Project-Based: "Project-Phoenix", "Initiative-Q3-2024"
Content-Type: "Sprint-Reviews", "Customer-Calls", "Legal-Contracts"
Department: "Eng-Architecture", "Sales-Training", "HR-Interviews"
```

Avoid generic names that don't communicate purpose:
❌ "Important", "Misc", "Other", "Temp", "Group"
✓ "Executive-Briefings", "Tech-Specs", "Client-Demos"

### Retention Strategy

Set thoughtful defaults that balance storage costs with compliance needs:

```
Global Default: 90 days (captures most content)
Legal Group Tags: 2555 days (7 years for legal records)
Compliance Tags: Protected (permanent retention)
Meeting Tags: 180 days (reasonable collaboration window)
Stand-up Tags: 14 days (ephemeral daily content)
```

Review retention policies quarterly to ensure they remain appropriate as business needs change.

### Group Admin Distribution

Grant group admin roles to natural group leaders - project managers, department heads, tech leads. This distributes administrative workload and empowers groups to self-manage.

Avoid granting group admin too liberally. While it's scoped to individual groups, group admins can add members and create tags that affect access. Limit the role to trusted individuals who understand security implications.

Document each group's admins in the group description or external documentation. Future administrators will appreciate knowing who to contact about group-specific questions.

## Integration with Other Features

### Inquire Mode

Group tags automatically appear in Inquire Mode's available filters, enabling group-scoped semantic search. Users can search for "budget discussions" and filter to just their project group, finding relevant conversations without noise from other groups.

Recordings shared via group tags are included in semantic search results. The vector store indexes all accessible recordings, meaning group content becomes part of users' searchable knowledge base automatically.

### Retention and Auto-Deletion

Tag-level retention policies integrate with Speakr's auto-deletion system. The nightly retention check evaluates each recording's tags to determine applicable retention periods:

1. If recording has protected tags → Never deleted
2. If recording has tags with `retention_days` → Use shortest tag retention
3. Otherwise → Use global `GLOBAL_RETENTION_DAYS`

This cascading system lets groups set specific policies while maintaining instance-wide defaults for untagged content.

### Public Sharing

Group membership doesn't affect public sharing capabilities. Users' ability to create public share links is controlled by:

1. Global `ENABLE_PUBLIC_SHARING` setting
2. Per-user `can_share_publicly` permission (if global is enabled)

Group members can create public links for group recordings if they have appropriate permissions, enabling external stakeholder communication while maintaining group-internal collaboration.

---

Groups transform multi-user Speakr instances into collaborative platforms where information flows automatically to relevant people. Proper configuration and management ensure security while enabling seamless knowledge sharing.

For user-focused group documentation, see the [Group Collaboration](../user-guide/groups.md) guide.

Return to [Admin Guide](index.md) →
