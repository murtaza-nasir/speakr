import { describe, it, expect } from 'vitest';
import { UPLOAD_ACCEPT, isAcceptedUploadFile, isVideoFileName } from './upload-file-type.js';
import { isVideoFileName as joinIsVideoFileName } from './upload-join.js';

const file = (name, type = '') => ({ name, type });
const acceptExtensions = UPLOAD_ACCEPT.split(',').filter(a => a.startsWith('.')).map(a => a.slice(1));

describe('isAcceptedUploadFile', () => {
    it('queues Ogg audio whatever type the browser gives it (#510)', () => {
        for (const type of ['audio/ogg', 'video/ogg', 'application/ogg', '']) {
            expect(isAcceptedUploadFile(file('example.ogg', type))).toBe(true);
        }
        expect(isAcceptedUploadFile(file('clip.oga'))).toBe(true);
        expect(isAcceptedUploadFile(file('clip.opus'))).toBe(true);
    });

    it('queues every extension the picker offers, with no browser type', () => {
        for (const ext of acceptExtensions) {
            expect(isAcceptedUploadFile(file(`clip.${ext}`)), ext).toBe(true);
            expect(isAcceptedUploadFile(file(`CLIP.${ext.toUpperCase()}`)), ext).toBe(true);
        }
    });

    it('queues any audio or video type', () => {
        expect(isAcceptedUploadFile(file('a.mp3', 'audio/mpeg'))).toBe(true);
        expect(isAcceptedUploadFile(file('a', 'video/x-ms-asf'))).toBe(true);
        expect(isAcceptedUploadFile(file('meeting.ts', 'text/vnd.trolltech.linguist'))).toBe(true);
    });

    it('skips documents, pages and programs', () => {
        for (const [name, type] of [
            ['notes.txt', 'text/plain'], ['page.html', 'text/html'], ['page.htm', ''],
            ['image.svg', 'image/svg+xml'], ['run.exe', 'application/x-msdownload'],
            ['script.js', 'text/javascript'], ['archive.zip', 'application/zip'],
            ['noextension', ''], ['ogg', ''], ['clip.ogg.html', 'text/html'],
        ]) {
            expect(isAcceptedUploadFile(file(name, type)), name).toBe(false);
        }
        expect(isAcceptedUploadFile(null)).toBe(false);
    });
});

describe('isVideoFileName', () => {
    it('sizes video containers against the video limit and audio against the regular one', () => {
        for (const name of ['a.mp4', 'a.mov', 'a.webm', 'a.ogv', 'a.mpeg', 'a.mpg', 'a.vob', 'a.asf', 'a.m4v', 'a.ts']) {
            expect(isVideoFileName(name), name).toBe(true);
        }
        for (const name of ['a.ogg', 'a.oga', 'a.opus', 'a.weba', 'a.mp3', 'a.3gp', '', undefined]) {
            expect(isVideoFileName(name), String(name)).toBe(false);
        }
    });

    it('is the same list the join helper uses', () => {
        for (const ext of acceptExtensions) {
            expect(joinIsVideoFileName(`x.${ext}`)).toBe(isVideoFileName(`x.${ext}`));
        }
    });
});

describe('UPLOAD_ACCEPT', () => {
    it('offers audio and video types and the listed extensions', () => {
        expect(UPLOAD_ACCEPT.split(',')).toEqual(expect.arrayContaining(['audio/*', 'video/*', '.ogg', '.oga', '.opus', '.mpeg']));
        expect(UPLOAD_ACCEPT).not.toMatch(/html|svg|\.js\b|text\//);
    });
});
