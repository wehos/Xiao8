import type { SyntheticEvent } from 'react';
import SmartTextBlock from './SmartTextBlock';
import { isMemeProxyImageUrl, swapImageToMemeLoadFailedSticker } from './memeImageFallback';
import { normalizeExternalUrlHref, openExternalUrl } from './openExternal';
import {
  type ChatMessage,
  type MessageAction,
  type MessageBlock,
} from './message-schema';

type MessageBlockViewProps = {
  block: MessageBlock;
  message: ChatMessage;
  isStreaming?: boolean;
  onAction?: (message: ChatMessage, action: MessageAction) => void;
};

const MUSIC_COVER_PLACEHOLDER_URL = '/static/assets/music/music-cover-placeholder.png';

function handleImageLoadError(event: SyntheticEvent<HTMLImageElement>, url: string) {
  swapImageToMemeLoadFailedSticker(event.currentTarget, url);
}

function handleLinkThumbnailLoadError(
  event: SyntheticEvent<HTMLImageElement>,
  messageId: ChatMessage['id'],
) {
  if (typeof messageId !== 'string' || !messageId.startsWith('music-')) return;

  const image = event.currentTarget;
  const placeholderUrl = new URL(MUSIC_COVER_PLACEHOLDER_URL, window.location.href).href;
  if (image.src === placeholderUrl) return;
  image.src = MUSIC_COVER_PLACEHOLDER_URL;
}

export function isGuideMessage(message: ChatMessage) {
  return typeof message.id === 'string' && message.id.startsWith('yui-guide-');
}

export default function MessageBlockView({
  block,
  message,
  isStreaming,
  onAction,
}: MessageBlockViewProps) {
  if (block.type === 'text') {
    return (
      <SmartTextBlock
        text={block.text}
        isStreaming={isStreaming}
        disableStreamingReveal={isGuideMessage(message)}
      />
    );
  }

  if (block.type === 'image') {
    const isMemeProxyImage = isMemeProxyImageUrl(block.url);
    const imageLoadingProps = isMemeProxyImage
      ? { loading: 'eager' as const, fetchpriority: 'high' as const }
      : { loading: 'lazy' as const };

    return (
      <figure
        className="message-block message-block-image"
        style={block.width && block.height ? { aspectRatio: `${block.width} / ${block.height}` } : undefined}
      >
        <img
          src={block.url}
          alt={block.alt || ''}
          {...imageLoadingProps}
          onError={(event) => handleImageLoadError(event, block.url)}
        />
      </figure>
    );
  }

  if (block.type === 'link') {
    const safeHref = normalizeExternalUrlHref(block.url);
    return (
      <a
        className="message-block message-block-link"
        href={safeHref || undefined}
        target={safeHref ? '_blank' : undefined}
        rel={safeHref ? 'noreferrer' : undefined}
        onClick={(event) => {
          event.preventDefault();
          openExternalUrl(block.url);
        }}
      >
        {block.thumbnailUrl ? (
          <div className="message-link-thumb">
            <img
              key={block.thumbnailUrl}
              src={block.thumbnailUrl}
              alt=""
              loading="lazy"
              onError={(event) => handleLinkThumbnailLoadError(event, message.id)}
            />
          </div>
        ) : null}
        <div className="message-link-copy">
          <div className="message-link-title">{block.title || block.url}</div>
          {block.description ? <div className="message-link-description">{block.description}</div> : null}
          <div className="message-link-url">{block.siteName || block.url}</div>
        </div>
      </a>
    );
  }

  if (block.type === 'status') {
    return (
      <div className={`message-block message-block-status tone-${block.tone || 'info'}`}>
        {block.text}
      </div>
    );
  }

  if (block.type === 'buttons') {
    return (
      <div className="message-block message-block-buttons">
        {block.buttons.map((action) => (
          <button
            key={action.id}
            className={`message-action-button variant-${action.variant || 'secondary'}`}
            type="button"
            disabled={action.disabled}
            onClick={() => onAction?.(message, action)}
          >
            {action.label}
          </button>
        ))}
      </div>
    );
  }

  return null;
}
