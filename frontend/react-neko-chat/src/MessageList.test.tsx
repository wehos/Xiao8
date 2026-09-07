import { fireEvent, render } from '@testing-library/react';
import MessageList from './MessageList';
import { MEME_IMAGE_LOAD_FAILED_STICKER_URL } from './memeImageFallback';
import { parseChatMessage } from './message-schema';

const message = parseChatMessage({
  id: 'm1',
  role: 'assistant',
  author: 'Neko',
  time: '10:00',
  createdAt: 1,
  blocks: [{ type: 'text', text: 'hi' }],
  status: 'sent',
});

describe('MessageList 凝神 thinking-dots', () => {
  it('appends a thinking-dots bubble at the tail only when thinking', () => {
    const { container, rerender } = render(<MessageList messages={[message]} />);
    expect(container.querySelector('.focus-thinking-row')).toBeNull();

    rerender(<MessageList messages={[message]} thinking />);
    const row = container.querySelector('.focus-thinking-row');
    expect(row).not.toBeNull();
    expect(row?.getAttribute('data-focus-thinking')).toBe('true');
    expect(row?.querySelectorAll('.focus-thinking-dot').length).toBe(3);

    // It is the LAST row so it reads as a pending reply after the messages.
    const rows = container.querySelectorAll('.message-row');
    expect(rows[rows.length - 1]).toBe(row);
  });

  it('still shows the thinking-dots bubble when the history is empty', () => {
    const { container } = render(<MessageList messages={[]} thinking />);
    const row = container.querySelector('.focus-thinking-row');
    expect(row).not.toBeNull();
    expect(row?.querySelectorAll('.focus-thinking-dot').length).toBe(3);
  });
});

describe('MessageList image fallback', () => {
  it('keeps a normal image URL until the browser reports a load error', () => {
    const imageMessage = parseChatMessage({
      id: 'img-1',
      role: 'assistant',
      author: 'Neko',
      time: '10:01',
      createdAt: 2,
      blocks: [{ type: 'image', url: '/api/meme/proxy-image?url=ok', alt: 'ok meme' }],
      status: 'sent',
    });
    const { container } = render(<MessageList messages={[imageMessage]} />);
    const img = container.querySelector<HTMLImageElement>('.message-block-image img');

    expect(img).not.toBeNull();
    expect(img).toHaveAttribute('src', '/api/meme/proxy-image?url=ok');
    expect(img).toHaveAttribute('loading', 'eager');
    expect(img).toHaveAttribute('fetchpriority', 'high');
    expect(img).not.toHaveAttribute('data-neko-image-load-failed-sticker');

    fireEvent.error(img as HTMLImageElement);

    expect(img).toHaveAttribute('src', MEME_IMAGE_LOAD_FAILED_STICKER_URL);
    expect(img).toHaveAttribute('data-neko-image-load-failed-sticker', 'true');
  });

  it('does not use the meme failed sticker for non-meme images', () => {
    const imageMessage = parseChatMessage({
      id: 'img-2',
      role: 'assistant',
      author: 'Neko',
      time: '10:02',
      createdAt: 3,
      blocks: [{ type: 'image', url: '/static/icons/cat_icon.png', alt: 'regular image' }],
      status: 'sent',
    });
    const { container } = render(<MessageList messages={[imageMessage]} />);
    const img = container.querySelector<HTMLImageElement>('.message-block-image img');

    expect(img).not.toBeNull();
    expect(img).toHaveAttribute('src', '/static/icons/cat_icon.png');
    expect(img).toHaveAttribute('loading', 'lazy');
    expect(img).not.toHaveAttribute('fetchpriority');

    fireEvent.error(img as HTMLImageElement);

    expect(img).toHaveAttribute('src', '/static/icons/cat_icon.png');
    expect(img).not.toHaveAttribute('src', MEME_IMAGE_LOAD_FAILED_STICKER_URL);
    expect(img).not.toHaveAttribute('data-neko-image-load-failed-sticker');
  });
});

describe('MessageList music cover fallback', () => {
  const placeholderUrl = '/static/assets/music/music-cover-placeholder.png';

  it('replaces a failed music thumbnail with the local placeholder without looping', () => {
    const musicMessage = parseChatMessage({
      id: 'music-cover-1',
      role: 'assistant',
      author: 'Neko',
      time: '10:03',
      createdAt: 4,
      blocks: [{
        type: 'link',
        url: 'https://music.example/track',
        title: 'Track',
        thumbnailUrl: 'https://music.example/cover.jpg',
      }],
      status: 'sent',
    });
    const { container } = render(<MessageList messages={[musicMessage]} />);
    const img = container.querySelector<HTMLImageElement>('.message-link-thumb img');

    expect(img).not.toBeNull();
    expect(img).toHaveAttribute('src', 'https://music.example/cover.jpg');

    fireEvent.error(img as HTMLImageElement);
    expect(img).toHaveAttribute('src', placeholderUrl);

    fireEvent.error(img as HTMLImageElement);
    expect(img).toHaveAttribute('src', placeholderUrl);
  });

  it('does not replace a failed thumbnail on a regular link message', () => {
    const linkMessage = parseChatMessage({
      id: 'link-cover-1',
      role: 'assistant',
      author: 'Neko',
      time: '10:04',
      createdAt: 5,
      blocks: [{
        type: 'link',
        url: 'https://example.com/article',
        title: 'Article',
        thumbnailUrl: 'https://example.com/thumbnail.jpg',
      }],
      status: 'sent',
    });
    const { container } = render(<MessageList messages={[linkMessage]} />);
    const img = container.querySelector<HTMLImageElement>('.message-link-thumb img');

    fireEvent.error(img as HTMLImageElement);

    expect(img).toHaveAttribute('src', 'https://example.com/thumbnail.jpg');
  });

  it('remounts the thumbnail when a reused music card switches candidates', () => {
    const firstCandidate = parseChatMessage({
      id: 'music-cover-2',
      role: 'assistant',
      author: 'Neko',
      time: '10:05',
      createdAt: 6,
      blocks: [{
        type: 'link',
        url: 'https://music.example/first',
        title: 'First',
        thumbnailUrl: 'https://music.example/first.jpg',
      }],
      status: 'sent',
    });
    const secondCandidate = parseChatMessage({
      ...firstCandidate,
      blocks: [{
        type: 'link',
        url: 'https://music.example/second',
        title: 'Second',
        thumbnailUrl: 'https://music.example/second.jpg',
      }],
    });
    const { container, rerender } = render(<MessageList messages={[firstCandidate]} />);
    const firstImage = container.querySelector<HTMLImageElement>('.message-link-thumb img');

    rerender(<MessageList messages={[secondCandidate]} />);
    const secondImage = container.querySelector<HTMLImageElement>('.message-link-thumb img');

    expect(secondImage).not.toBe(firstImage);
    expect(secondImage).toHaveAttribute('src', 'https://music.example/second.jpg');
  });
});

describe('plugin system bubble', () => {
  const pluginMessage = parseChatMessage({
    id: 'plugin-1',
    role: 'system',
    author: 'LifeKit',
    time: '10:05',
    createdAt: 2,
    blocks: [{ type: 'text', text: '哇你被打下来了！' }],
    status: 'sent',
  });

  it('names its source instead of wearing the character identity', () => {
    const { container } = render(<MessageList messages={[pluginMessage]} />);

    // A plugin may phrase its text in her voice — warthunder does, for
    // latency — so the source label is the only thing telling the reader
    // these words are not hers. For blind pushes she has no memory of them.
    const source = container.querySelector('.system-chip-source');
    expect(source?.textContent).toBe('LifeKit');
    expect(container.querySelector('.system-chip-content')?.textContent).toContain(
      '哇你被打下来了！',
    );

    // None of the character's identity may leak in.
    expect(container.querySelector('.avatar-assistant')).toBeNull();
    expect(container.querySelector('.message-bubble-assistant')).toBeNull();
    expect(container.querySelector('.message-row-system')).not.toBeNull();
  });

  it('falls back to the source kind when the plugin gave no name', () => {
    const unlabelled = parseChatMessage({
      id: 'plugin-2',
      role: 'system',
      // The schema requires a NON-EMPTY author, so the adapter substitutes the
      // source kind rather than leaving it blank — every plugin bubble is
      // labelled, none silently fails validation.
      author: 'plugin',
      time: '10:06',
      createdAt: 3,
      blocks: [{ type: 'text', text: 'session started' }],
      status: 'sent',
    });

    const { container } = render(<MessageList messages={[unlabelled]} />);

    expect(
      container.querySelector('.system-chip-source')?.textContent,
    ).toBe('plugin');
    expect(container.querySelector('.system-chip')).not.toBeNull();
  });
});
