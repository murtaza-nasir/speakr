---
layout: default
title: Group Collaboration
parent: User Guide
nav_order: 6
---

# Group Collaboration

Groups transform how groups work together in Speakr, enabling seamless collaboration without manual sharing overhead. When your organization uses groups, recordings automatically reach the right people at the right time, eliminating the tedious process of individually sharing each conversation with every relevant group member.

## Understanding Groups

Groups in Speakr are organized groups of users who regularly need access to the same recordings. Unlike individual sharing where you manually grant access user-by-user, groups use smart tags that automatically share content with all group members when applied. This automation makes groups perfect for departments, project groups, or any collection of people who work together regularly.

Your administrator creates and manages groups through the admin dashboard. Once you're added to a group, you gain access to group-specific tags and automatically receive recordings when teammates apply group tags. This creates a collaborative space where everyone stays informed without constant manual intervention.

Groups work alongside personal organization tools. Your personal tags remain private and independent, while group tags create shared context. You can combine both - use personal tags for your workflow organization and group tags for automatic collaboration, creating a system that serves both individual and group needs.

## Group Tags

Group tags are the engine that powers group collaboration. They look and work like regular tags but carry special automatic sharing capabilities. When anyone applies a group tag to a recording, every group member instantly gains access without additional steps.

### Recognizing Group Tags

Group tags display with a distinctive users icon (👥) throughout the Speakr interface, making them immediately identifiable. Whether you're browsing the tag selector, viewing a recording's tags, or managing tags in settings, the users icon clearly marks group versus personal tags.

In your tag management interface, group tags show the group name and cannot be deleted by regular users. Only group admins can create or remove group tags, ensuring centralized control over group-wide organizational systems. This prevents confusion where different group members create conflicting organizational structures.

### Applying Group Tags

Applying a group tag works exactly like applying a personal tag - open a recording, click the tags button, and select from your available tags. The difference happens behind the scenes: the moment you apply a group tag, Speakr automatically creates internal shares for every group member.

This automatic sharing grants group members both view and edit access. They can read transcriptions and summaries, listen to audio, modify notes and metadata, and add their own tags. However, even with edit access, only the recording's original owner can delete it. This balance enables collaboration while protecting content ownership.

The automatic sharing is intelligent. If a group member already has access through a previous share, Speakr doesn't create duplicates. If someone is added to the group later, they don't automatically gain access to old recordings - only recordings tagged after they join. This prevents retroactive access leakage while maintaining security.

### Using Group Tags Effectively

Apply group tags immediately after important meetings or discussions to ensure group-wide visibility while context is fresh. This creates a shared understanding quickly, letting everyone review content and add notes while details are clear. Prompt tagging turns recordings from individual resources into group knowledge.

Combine group tags with personal tags for powerful organization. Use group tags to identify the audience (which group needs access) and personal tags for workflow status or content type. For example, tag a recording with both the "Engineering" group tag and your personal "Action Required" tag. The group tag shares with colleagues, while your personal tag tracks your follow-ups.

When recordings touch multiple groups, you can apply multiple group tags. Each group tag grants access to its respective group members, creating overlapping access that matches cross-functional work. Be thoughtful with multi-group tagging - ensure all tagged groups actually need access to avoid overcommunicating.

### Group Tag Features

Some group tags carry special capabilities beyond basic sharing. Your administrator might configure group tags with custom retention policies that override global deletion rules. A "Legal Records" group tag might preserve recordings for seven years, while a "Daily Stand-ups" tag might auto-delete after two weeks. These tag-specific retention policies happen automatically - you just apply the tag and the system enforces the appropriate lifecycle.

Protected group tags prevent automatic deletion entirely, regardless of age. Recordings with protected tags are never auto-deleted, making them perfect for compliance requirements, important decisions, or reference material. The protection is permanent until someone removes the tag, ensuring critical content remains accessible.

Group tags can also include custom AI prompts that generate specialized summaries. When you apply a tag with a custom prompt, reprocessing the summary uses that prompt instead of your default. This lets different types of group content receive appropriate AI treatment - technical reviews get detailed technical summaries, while executive briefings get high-level overviews.

## Working with Group Recordings

Recordings shared via group tags appear in your main interface alongside your own recordings. Clear visual indicators help distinguish content sources and access levels, so you always know what you're looking at and what you can do with it.

### Identifying Group Content

The recording cards show multiple badges that communicate status at a glance. A blue "Group" badge indicates the recording has group tags applied, signaling it's group content. If you don't own the recording, a purple "Shared" badge appears, showing you've received access through sharing. Hovering over shared recordings reveals the owner's username, maintaining clarity about content source.

Group tags themselves appear on recording cards, each marked with the users icon. When you see multiple tags on a recording, the icon helps identify which are group-wide (accessible to all group members) versus personal tags (visible only to the owner).

The "Shared with Me" filter in the sidebar isolates recordings others have shared with you, including those shared via group tags. This filtered view helps focus on collaborative content when you need to review group discussions without distraction from your personal recordings.

### Your Permissions on Group Recordings

As a group member, you receive edit permissions on recordings with group tags. This means you can modify the title to add clarity, update the participant list with correct names, adjust the meeting date if needed, edit notes to add context or corrections, and add tags for better organization.

Your edit capabilities come with important limitations. You cannot delete group recordings even with edit access - only the original owner can delete content. This prevents accidental data loss while enabling collaborative enhancement. You also cannot share group recordings with users outside the group without re-share permissions, which are typically reserved for group administrators.

### Personal Notes on Group Recordings

When you access a group recording, you can add personal notes that remain completely private. These personal notes never appear to the recording owner or other group members - they're your private space for observations, follow-ups, questions, or action items.

Personal notes persist as long as you have access to the recording. If you leave the group or a group admin revokes your access, your personal notes are automatically deleted. This cleanup prevents orphaned data and ensures notes don't persist after access ends.

Use personal notes to track what matters to you without cluttering shared notes. The recording owner's notes should contain universal context, while your personal notes capture your specific perspective, tasks, or concerns. This separation keeps shared notes focused while giving everyone space for individual tracking.

## Group Membership

Your group membership determines which group tags you can see and which recordings you automatically receive when teammates tag content. Group administrators can adjust membership as projects and roles evolve.

### Joining a Group

Group membership is managed by administrators through the admin dashboard. When you're added to a group, you immediately gain access to that group's tags and can apply them to your recordings. You also begin receiving recordings when teammates use group tags going forward.

New group membership doesn't grant retroactive access. If a group has been using group tags before you joined, you won't automatically see older group-tagged recordings. This prevents information leakage and maintains security. Group admins can manually share historical recordings if needed.

### Group Roles

Groups have two roles that determine capabilities within the group structure. Regular members can use group tags on their recordings and access recordings tagged by others. They see group content, contribute their recordings to the group space, and participate in collaborative discussions.

Group admins have additional responsibilities. They can add or remove group members, promoting collaboration flexibility. They can create new group tags with custom settings, defining the organizational structure for group content. They can modify or delete existing group tags, maintaining the group's tag taxonomy as needs evolve. For administrators, group admins can access a limited version of the admin dashboard focused solely on group management.

### Leaving Groups

When you leave a group (either voluntarily or through admin action), several things happen automatically. You lose access to group tags, so they disappear from your tag selectors. Your access to group-shared recordings continues if you received them before leaving, but you won't receive new group recordings going forward. Any personal notes you created on group recordings are preserved if you retain access through other sharing mechanisms.

## Group Administration for Group Admins

If you're a group admin rather than a regular member, you have access to group management capabilities through a special admin interface. This limited admin dashboard focuses exclusively on group management without exposing system-wide administrative features.

### Accessing Group Management

Group admins see a "Group Management" link in their account menu, replacing the regular admin link. Clicking this takes you to a focused interface showing only groups you administer. You cannot see or manage other groups, maintaining security boundaries between different group administrators.

The group management interface lets you view group members with their roles, add new members from your organization, change member roles between admin and regular member, remove members who no longer need access, and manage group tags including retention and protection settings.

### Managing Group Members

Adding members to your group makes them immediately eligible to receive group-tagged recordings. Search for users by username, select their initial role, and add them. They gain access to group tags right away and will receive future group recordings.

Changing roles between member and admin adjusts capabilities. Promoting a member to admin grants them group management abilities, useful for distributing administrative workload. Demoting an admin to member removes management capabilities while preserving their group access.

Removing members cuts off their access to future group recordings. They keep access to recordings they've already received, but new group tags won't share with them anymore. If you need to fully revoke access to existing content, you'll need to manually revoke those internal shares.

### Creating and Managing Group Tags

Group tags are your group's organizational vocabulary. Create tags that match how your group thinks about content - project names, content types, meeting categories, or whatever organizational scheme makes sense for your work.

When creating group tags, you can set custom retention periods that override global settings. This lets your group manage content lifecycle based on group-specific requirements. Legal groups might set long retention, while operations groups might prefer shorter retention for routine recordings.

Protection flags prevent automatic deletion entirely. Use protection for recordings that must be kept permanently - compliance records, critical decisions, reference materials. Protected tags ensure content survives regardless of age or global retention policies.

### Syncing Group Shares

If your group started using group tags before automatic sharing was fully implemented, you might have tagged recordings that didn't automatically share with current group members. The "Sync Group Shares" feature retroactively applies automatic sharing to existing group-tagged recordings.

This sync operation runs through all recordings with your group's tags and creates internal shares for current group members who don't already have access. It's a one-time operation that brings historical content into alignment with current group membership, useful after enabling new group features or fixing configuration issues.

## Best Practices for Group Collaboration

Start using group tags from the beginning of projects or group formation. This builds a complete record of group discussions from inception, creating valuable historical context. Retroactively organizing recordings is possible but more tedious than tagging promptly.

Establish group conventions for when to use group tags versus individual sharing. Some content naturally belongs to the whole group, while other recordings might be relevant to specific individuals. Clear guidelines prevent over-sharing while ensuring important information reaches everyone who needs it.

Use descriptive group tag names that clearly communicate content type or purpose. Names like "Sprint Planning" or "Customer Feedback" immediately convey content without requiring detective work. Avoid generic tags like "Important" or "Misc" that don't add meaningful organization.

Review group tags periodically with your group admin to ensure they remain relevant. Projects end, organizational structures change, and tag systems that once made sense can become outdated. Regular maintenance keeps group organization clean and useful.

Contribute to shared notes when you add valuable context, but avoid duplicating information. Read existing notes before adding your own to prevent redundant content. Well-maintained shared notes become valuable group resources, while cluttered notes lose utility.

Use personal notes liberally for your individual tracking needs. Don't worry about perfect notes since only you see them. Personal notes are your space for rough thoughts, action items, questions, and observations that might not be polished enough for shared notes.

## Integration with Inquire Mode

When your instance has Inquire Mode enabled, groups enhance semantic search capabilities. Group tags appear in Inquire Mode's filter options, letting you scope searches to specific group contexts. Search for "budget discussion" and filter to just your project group, finding relevant conversations without noise from other groups.

Recordings shared via group tags are automatically included in your Inquire Mode searches. This means semantic search spans both your personal recordings and group content, creating a comprehensive knowledge base. The AI understands context across all accessible recordings, providing better answers by drawing on collective group knowledge.

Your personal notes on group recordings remain private even in Inquire Mode. When semantic search uses a group recording to answer questions, it draws on the transcription and shared notes, but never exposes your personal annotations. This maintains the privacy boundary while enabling powerful group-wide search.

---

Ready to start collaborating? If you're not yet part of any groups, contact your administrator about joining relevant groups. If you're a group admin, learn more in the [Group Management](../admin-guide/group-management.md) guide.

Return to [User Guide](index.md) →
