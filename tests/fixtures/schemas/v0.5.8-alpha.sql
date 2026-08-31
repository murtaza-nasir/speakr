-- Schema of Speakr v0.5.8-alpha, generated from that tag's own models.
-- Regenerate with scripts/generate_schema_fixtures.py. Do not hand-edit.
-- user table: 14 columns

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
	meeting_date DATE, 
	file_size INTEGER, 
	original_filename VARCHAR(500), 
	is_inbox BOOLEAN, 
	is_highlighted BOOLEAN, 
	mime_type VARCHAR(100), 
	completed_at DATETIME, 
	processing_time_seconds INTEGER, 
	processing_source VARCHAR(50), 
	error_message TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
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

CREATE TABLE speaker (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	user_id INTEGER NOT NULL, 
	created_at DATETIME, 
	last_used DATETIME, 
	use_count INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id)
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
	color VARCHAR(7), 
	custom_prompt TEXT, 
	default_language VARCHAR(10), 
	default_min_speakers INTEGER, 
	default_max_speakers INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT _user_tag_uc UNIQUE (name, user_id), 
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

CREATE TABLE user (
	id INTEGER NOT NULL, 
	username VARCHAR(20) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	password VARCHAR(60) NOT NULL, 
	is_admin BOOLEAN, 
	transcription_language VARCHAR(10), 
	output_language VARCHAR(50), 
	ui_language VARCHAR(10), 
	summary_prompt TEXT, 
	extract_events BOOLEAN, 
	name VARCHAR(100), 
	job_title VARCHAR(100), 
	company VARCHAR(100), 
	diarize BOOLEAN, 
	PRIMARY KEY (id), 
	UNIQUE (username), 
	UNIQUE (email)
);
