const test = require('node:test');
const assert = require('node:assert/strict');

const layout = require('../../static/js/card_maker_embed_layout.js');

test('uses the normalized forge left-lane anchor for VRM and PNGTuber', () => {
    const frame = layout.resolveFrame(1470, 770, 390, 770);

    assert.equal(frame.centerX, 1470 * 0.25);
    assert.equal(frame.centerY, 770 * 0.66);
    assert.equal(frame.scale, 1.25);
    assert.equal(frame.maxWidth, 1470 * 0.5);
});

test('fits unusually wide models inside the left forge boundary', () => {
    const frame = layout.resolveFrame(1470, 770, 1000, 770);

    assert.equal(frame.scale, frame.maxWidth / 1000);
    assert.ok(1000 * frame.scale <= 1470 * 0.5);
    assert.ok(770 * frame.scale <= 770 * 1.25);
});

test('preserves PNGTuber aspect ratio before applying the shared frame', () => {
    const contained = layout.resolveContainedSize(1470, 770, 3678, 4182);

    assert.equal(contained.height, 770);
    assert.ok(Math.abs(contained.width / contained.height - 3678 / 4182) < 1e-12);
});

test('perspective framing keeps narrow and wide 3D models on the same screen anchor', () => {
    for (const modelWidth of [0.5, 1.5, 4]) {
        const frame = layout.resolvePerspectiveFrame(1470, 770, modelWidth, 2, Math.PI / 3);
        const projectedCenterX = (frame.ndcX + 1) / 2;
        const projectedCenterY = (1 - frame.ndcY) / 2;
        const projectedWidth = modelWidth / (2 * frame.halfViewWidth);
        const projectedHeight = 2 / (2 * frame.halfViewHeight);

        assert.ok(Math.abs(projectedCenterX - 0.25) < 1e-12);
        assert.ok(Math.abs(projectedCenterY - 0.66) < 1e-12);
        assert.ok(projectedWidth <= 0.5 + 1e-12);
        assert.ok(projectedHeight <= 1.25 + 1e-12);
    }
});
