(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.NEKOCardMakerEmbedLayout = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    // These values describe YUI's visible pixels in the previous canvas-based
    // framing (roughly 25.4%, 66.2%, and 1.253x tall), rounded into a stable
    // provider-independent forge coordinate system.
    const HEIGHT_RATIO = 1.25;
    const CENTER_X_RATIO = 0.25;
    const CENTER_Y_RATIO = 0.66;
    // Mirroring the center distance keeps every visible model inside the
    // complete left half of the stage, before the forge machine begins.
    const MAX_WIDTH_RATIO = CENTER_X_RATIO * 2;

    function positiveNumber(value, fallback = 1) {
        const number = Number(value);
        return Number.isFinite(number) && number > 0 ? number : fallback;
    }

    function resolveFrame(viewportWidth, viewportHeight, contentWidth, contentHeight) {
        const width = positiveNumber(viewportWidth);
        const height = positiveNumber(viewportHeight);
        const modelWidth = positiveNumber(contentWidth);
        const modelHeight = positiveNumber(contentHeight);
        const heightScale = (height * HEIGHT_RATIO) / modelHeight;
        const widthScale = (width * MAX_WIDTH_RATIO) / modelWidth;

        return {
            centerX: width * CENTER_X_RATIO,
            centerY: height * CENTER_Y_RATIO,
            maxWidth: width * MAX_WIDTH_RATIO,
            maxHeight: height * HEIGHT_RATIO,
            scale: Math.min(heightScale, widthScale)
        };
    }

    function resolveContainedSize(viewportWidth, viewportHeight, sourceWidth, sourceHeight) {
        const width = positiveNumber(viewportWidth);
        const height = positiveNumber(viewportHeight);
        const assetWidth = positiveNumber(sourceWidth);
        const assetHeight = positiveNumber(sourceHeight);
        const containScale = Math.min(width / assetWidth, height / assetHeight);

        return {
            width: assetWidth * containScale,
            height: assetHeight * containScale
        };
    }

    function resolvePerspectiveFrame(viewportWidth, viewportHeight, contentWidth, contentHeight, verticalFovRadians) {
        const width = positiveNumber(viewportWidth);
        const height = positiveNumber(viewportHeight);
        const modelWidth = positiveNumber(contentWidth);
        const modelHeight = positiveNumber(contentHeight);
        const fov = positiveNumber(verticalFovRadians, Math.PI / 4);
        const tangent = Math.max(0.000001, Math.tan(fov / 2));
        const aspect = width / height;
        const heightDistance = modelHeight / (2 * tangent * HEIGHT_RATIO);
        const widthDistance = modelWidth / (2 * tangent * aspect * MAX_WIDTH_RATIO);
        const distance = Math.max(heightDistance, widthDistance, 0.01);
        const halfViewHeight = distance * tangent;

        return {
            distance,
            ndcX: CENTER_X_RATIO * 2 - 1,
            ndcY: 1 - CENTER_Y_RATIO * 2,
            halfViewHeight,
            halfViewWidth: halfViewHeight * aspect
        };
    }

    return Object.freeze({
        HEIGHT_RATIO,
        CENTER_X_RATIO,
        CENTER_Y_RATIO,
        MAX_WIDTH_RATIO,
        resolveFrame,
        resolveContainedSize,
        resolvePerspectiveFrame
    });
});
