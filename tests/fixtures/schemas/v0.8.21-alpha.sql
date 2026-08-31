-- Schema of Speakr v0.8.21-alpha, generated from that tag's own models.
-- Regenerate with scripts/generate_schema_fixtures.py. Do not hand-edit.
-- user table: 32 columns

CREATE TABLE api_token (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	name VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	last_used_at DATETIME, 
	expires_at DATETIME, 
	revoked BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE event (
	id INTEGER NOT NULL, 
	recording_id INTEGER NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	description TEXT, 
	start_datetime DATETIME NOT NULL, 
	end_datetime DATETIME, 
	location VARCHAR(500), 
	attendees TEXT, 
	reminder_minutes INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id)
);

CREATE TABLE export_template (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	template TEXT NOT NULL, 
	description VARCHAR(500), 
	is_default BOOLEAN, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE folder (
	id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	user_id INTEGER NOT NULL, 
	group_id INTEGER, 
	color VARCHAR(7), 
	custom_prompt TEXT, 
	default_language VARCHAR(10), 
	default_min_speakers INTEGER, 
	default_max_speakers INTEGER, 
	default_hotwords TEXT, 
	default_initial_prompt TEXT, 
	default_transcription_model VARCHAR(120), 
	protect_from_deletion BOOLEAN, 
	retention_days INTEGER, 
	auto_share_on_apply BOOLEAN, 
	share_with_group_lead BOOLEAN, 
	naming_template_id INTEGER, 
	export_template_id INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT _user_folder_uc UNIQUE (name, user_id), 
	FOREIGN KEY(user_id) REFERENCES user (id), 
	FOREIGN KEY(group_id) REFERENCES "group" (id) ON DELETE CASCADE, 
	FOREIGN KEY(naming_template_id) REFERENCES naming_template (id) ON DELETE SET NULL, 
	FOREIGN KEY(export_template_id) REFERENCES export_template (id) ON DELETE SET NULL
);

CREATE TABLE "group" (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE group_membership (
	id INTEGER NOT NULL, 
	group_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	role VARCHAR(20), 
	joined_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT unique_group_membership UNIQUE (group_id, user_id), 
	FOREIGN KEY(group_id) REFERENCES "group" (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);

CREATE TABLE inquire_session (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	session_name VARCHAR(200), 
	filter_tags TEXT, 
	filter_speakers TEXT, 
	filter_date_from DATE, 
	filter_date_to DATE, 
	filter_recording_ids TEXT, 
	created_at DATETIME, 
	last_used DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE internal_share (
	id INTEGER NOT NULL, 
	recording_id INTEGER NOT NULL, 
	owner_id INTEGER NOT NULL, 
	shared_with_user_id INTEGER NOT NULL, 
	can_edit BOOLEAN, 
	can_reshare BOOLEAN, 
	source_type VARCHAR(20), 
	source_tag_id INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT unique_recording_share UNIQUE (recording_id, shared_with_user_id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE, 
	FOREIGN KEY(owner_id) REFERENCES user (id) ON DELETE CASCADE, 
	FOREIGN KEY(shared_with_user_id) REFERENCES user (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_tag_id) REFERENCES tag (id) ON DELETE SET NULL
);

CREATE TABLE naming_template (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	template TEXT NOT NULL, 
	description VARCHAR(500), 
	regex_patterns TEXT, 
	is_default BOOLEAN, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE processing_job (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	recording_id INTEGER NOT NULL, 
	job_type VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	params TEXT, 
	error_message TEXT, 
	retry_count INTEGER NOT NULL, 
	is_new_upload BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE
);

CREATE TABLE push_subscriptions (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	endpoint VARCHAR(500) NOT NULL, 
	p256dh_key VARCHAR(200) NOT NULL, 
	auth_key VARCHAR(100) NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id), 
	UNIQUE (endpoint)
);

CREATE TABLE recording (
	user_id INTEGER, 
	id INTEGER NOT NULL, 
	title VARCHAR(200), 
	participants VARCHAR(500), 
	notes TEXT, 
	transcription TEXT, 
	summary TEXT, 
	status VARCHAR(50), 
	audio_path VARCHAR(500), 
	created_at DATETIME, 
	meeting_date DATETIME, 
	file_size INTEGER, 
	original_filename VARCHAR(500), 
	is_inbox BOOLEAN, 
	is_highlighted BOOLEAN, 
	mime_type VARCHAR(100), 
	completed_at DATETIME, 
	processing_time_seconds INTEGER, 
	transcription_duration_seconds INTEGER, 
	summarization_duration_seconds INTEGER, 
	processing_source VARCHAR(50), 
	error_message TEXT, 
	file_hash VARCHAR(64), 
	audio_deleted_at DATETIME, 
	deletion_exempt BOOLEAN, 
	speaker_embeddings JSON, 
	prompt_variables JSON, 
	folder_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id), 
	FOREIGN KEY(folder_id) REFERENCES folder (id) ON DELETE SET NULL
);

CREATE TABLE recording_tags (
	recording_id INTEGER NOT NULL, 
	tag_id INTEGER NOT NULL, 
	added_at DATETIME, 
	"order" INTEGER NOT NULL, 
	PRIMARY KEY (recording_id, tag_id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id), 
	FOREIGN KEY(tag_id) REFERENCES tag (id)
);

CREATE TABLE share (
	id INTEGER NOT NULL, 
	public_id VARCHAR(32) NOT NULL, 
	recording_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	created_at DATETIME, 
	share_summary BOOLEAN, 
	share_notes BOOLEAN, 
	PRIMARY KEY (id), 
	UNIQUE (public_id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE share_audit_log (
	id INTEGER NOT NULL, 
	action VARCHAR(20) NOT NULL, 
	recording_id INTEGER NOT NULL, 
	actor_id INTEGER NOT NULL, 
	target_user_id INTEGER, 
	permissions_granted JSON, 
	actor_permissions JSON, 
	timestamp DATETIME NOT NULL, 
	share_id INTEGER, 
	notes TEXT, 
	ip_address VARCHAR(45), 
	PRIMARY KEY (id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE, 
	FOREIGN KEY(actor_id) REFERENCES user (id), 
	FOREIGN KEY(target_user_id) REFERENCES user (id)
);

CREATE TABLE shared_recording_state (
	id INTEGER NOT NULL, 
	recording_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	personal_notes TEXT, 
	is_inbox BOOLEAN, 
	is_highlighted BOOLEAN, 
	last_viewed DATETIME, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT unique_user_recording_state UNIQUE (recording_id, user_id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);

CREATE TABLE speaker (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	user_id INTEGER NOT NULL, 
	created_at DATETIME, 
	last_used DATETIME, 
	use_count INTEGER, 
	average_embedding BLOB, 
	embeddings_history JSON, 
	embedding_count INTEGER, 
	confidence_score FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE speaker_snippet (
	id INTEGER NOT NULL, 
	speaker_id INTEGER NOT NULL, 
	recording_id INTEGER NOT NULL, 
	segment_index INTEGER NOT NULL, 
	text_snippet VARCHAR(200) NOT NULL, 
	timestamp FLOAT, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(speaker_id) REFERENCES speaker (id) ON DELETE CASCADE, 
	FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE
);

CREATE TABLE system_setting (
	id INTEGER NOT NULL, 
	"key" VARCHAR(100) NOT NULL, 
	value TEXT, 
	description TEXT, 
	setting_type VARCHAR(50) NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE ("key")
);

CREATE TABLE tag (
	id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	user_id INTEGER NOT NULL, 
	group_id INTEGER, 
	color VARCHAR(7), 
	custom_prompt TEXT, 
	default_language VARCHAR(10), 
	default_min_speakers INTEGER, 
	default_max_speakers INTEGER, 
	default_hotwords TEXT, 
	default_initial_prompt TEXT, 
	default_transcription_model VARCHAR(120), 
	protect_from_deletion BOOLEAN, 
	retention_days INTEGER, 
	auto_share_on_apply BOOLEAN, 
	share_with_group_lead BOOLEAN, 
	naming_template_id INTEGER, 
	export_template_id INTEGER, 
	is_auto_process BOOLEAN, 
	auto_process_folder_name VARCHAR(100), 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT _user_tag_uc UNIQUE (name, user_id), 
	FOREIGN KEY(user_id) REFERENCES user (id), 
	FOREIGN KEY(group_id) REFERENCES "group" (id) ON DELETE CASCADE, 
	FOREIGN KEY(naming_template_id) REFERENCES naming_template (id) ON DELETE SET NULL, 
	FOREIGN KEY(export_template_id) REFERENCES export_template (id) ON DELETE SET NULL
);

CREATE TABLE token_usage (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	date DATE NOT NULL, 
	operation_type VARCHAR(50) NOT NULL, 
	prompt_tokens INTEGER, 
	completion_tokens INTEGER, 
	total_tokens INTEGER, 
	cost FLOAT, 
	request_count INTEGER, 
	model_name VARCHAR(100), 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_date_op UNIQUE (user_id, date, operation_type), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE transcript_chunk (
	id INTEGER NOT NULL, 
	recording_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	chunk_index INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	start_time FLOAT, 
	end_time FLOAT, 
	speaker_name VARCHAR(100), 
	embedding BLOB, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(recording_id) REFERENCES recording (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE transcript_template (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	template TEXT NOT NULL, 
	description VARCHAR(500), 
	is_default BOOLEAN, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE transcription_usage (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	date DATE NOT NULL, 
	connector_type VARCHAR(50) NOT NULL, 
	audio_duration_seconds INTEGER, 
	estimated_cost FLOAT, 
	request_count INTEGER, 
	model_name VARCHAR(100), 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_date_connector UNIQUE (user_id, date, connector_type), 
	FOREIGN KEY(user_id) REFERENCES user (id)
);

CREATE TABLE user (
	id INTEGER NOT NULL, 
	username VARCHAR(20) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	password VARCHAR(60), 
	sso_provider VARCHAR(100), 
	sso_subject VARCHAR(255), 
	is_admin BOOLEAN, 
	can_share_publicly BOOLEAN, 
	transcription_language VARCHAR(10), 
	output_language VARCHAR(50), 
	ui_language VARCHAR(10), 
	summary_prompt TEXT, 
	extract_events BOOLEAN, 
	name VARCHAR(100), 
	job_title VARCHAR(100), 
	company VARCHAR(100), 
	diarize BOOLEAN, 
	default_naming_template_id INTEGER, 
	monthly_token_budget INTEGER, 
	monthly_transcription_budget INTEGER, 
	email_verified BOOLEAN, 
	email_verification_token VARCHAR(200), 
	email_verification_sent_at DATETIME, 
	password_reset_token VARCHAR(200), 
	password_reset_sent_at DATETIME, 
	auto_speaker_labelling BOOLEAN, 
	auto_speaker_labelling_threshold VARCHAR(10), 
	auto_summarization BOOLEAN, 
	transcription_hotwords TEXT, 
	transcription_initial_prompt TEXT, 
	show_timestamps_simple_view BOOLEAN, 
	editor_autosave BOOLEAN, 
	PRIMARY KEY (id), 
	UNIQUE (username), 
	UNIQUE (email), 
	UNIQUE (sso_subject), 
	FOREIGN KEY(default_naming_template_id) REFERENCES naming_template (id) ON DELETE SET NULL
);

CREATE INDEX idx_token_user_date ON token_usage (user_id, date);

CREATE INDEX idx_transcription_user_date ON transcription_usage (user_id, date);

CREATE INDEX idx_user_speaker_name ON transcript_chunk (user_id, speaker_name);

CREATE INDEX ix_api_token_revoked ON api_token (revoked);

CREATE UNIQUE INDEX ix_api_token_token_hash ON api_token (token_hash);

CREATE INDEX ix_processing_job_created_at ON processing_job (created_at);

CREATE INDEX ix_processing_job_recording_id ON processing_job (recording_id);

CREATE INDEX ix_processing_job_status ON processing_job (status);

CREATE INDEX ix_processing_job_user_id ON processing_job (user_id);

CREATE INDEX ix_recording_folder_id ON recording (folder_id);

CREATE INDEX ix_recording_user_file_hash ON "recording" (user_id, file_hash);

CREATE INDEX ix_transcript_chunk_speaker_name ON transcript_chunk (speaker_name);

CREATE INDEX ix_user_email_verification_token ON user (email_verification_token);

CREATE INDEX ix_user_password_reset_token ON user (password_reset_token);

CREATE UNIQUE INDEX ix_user_sso_subject ON "user" (sso_subject);
