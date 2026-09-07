'use strict';

// Execute the production enrollment AudioWorklet in a small Node host.
// PCM travels only through stdin/stdout; this helper never creates audio files.

const fs = require('node:fs');
const vm = require('node:vm');

const EXIT_CORPUS_INVALID = 3;
const EXIT_BROWSER_UNAVAILABLE = 6;
const WORKLET_QUANTUM_SAMPLES = 128;

function fail(exitCode) {
    process.exitCode = exitCode;
}

async function main() {
    const [, , workletPath, sourceRateText, targetRateText, targetSamplesText] = process.argv;
    const sourceRate = Number(sourceRateText);
    const targetRate = Number(targetRateText);
    const targetSamples = Number(targetSamplesText);
    if (
        !workletPath
        || !Number.isInteger(sourceRate)
        || sourceRate <= 0
        || !Number.isInteger(targetRate)
        || targetRate <= 0
        || !Number.isInteger(targetSamples)
        || targetSamples <= 0
    ) {
        fail(EXIT_BROWSER_UNAVAILABLE);
        return;
    }

    let registeredProcessor = null;
    const sandbox = {
        AudioWorkletProcessor: class {},
        Float32Array,
        Int16Array,
        Math,
        console: Object.freeze({
            log() {},
            warn() {},
            error() {},
        }),
        registerProcessor(_name, processorClass) {
            registeredProcessor = processorClass;
        },
    };

    try {
        const source = fs.readFileSync(workletPath, 'utf8');
        vm.createContext(sandbox);
        vm.runInContext(source, sandbox, { filename: workletPath });
    } catch (_error) {
        fail(EXIT_BROWSER_UNAVAILABLE);
        return;
    }
    if (typeof registeredProcessor !== 'function') {
        fail(EXIT_BROWSER_UNAVAILABLE);
        return;
    }

    const input = fs.readFileSync(0);
    if (input.length === 0 || input.length % 2 !== 0) {
        input.fill(0);
        fail(EXIT_CORPUS_INVALID);
        return;
    }

    const posted = [];
    let postedSamples = 0;
    let processor = null;
    let combined = null;
    let selected = null;
    try {
        processor = new registeredProcessor({
            processorOptions: {
                originalSampleRate: sourceRate,
                targetSampleRate: targetRate,
            },
        });
        processor.port = {
            postMessage(value) {
                if (!(value instanceof Int16Array)) {
                    throw new TypeError('worklet output must be Int16Array');
                }
                let copy = null;
                try {
                    copy = Buffer.from(
                        new Uint8Array(value.buffer, value.byteOffset, value.byteLength),
                    );
                    posted.push(copy);
                    postedSamples += value.length;
                    copy = null;
                } finally {
                    value.fill(0);
                    if (copy !== null) {
                        copy.fill(0);
                    }
                }
            },
        };

        const sourceSamples = input.length / 2;
        for (
            let offset = 0;
            offset < sourceSamples && postedSamples < targetSamples;
            offset += WORKLET_QUANTUM_SAMPLES
        ) {
            const count = Math.min(WORKLET_QUANTUM_SAMPLES, sourceSamples - offset);
            const block = new Float32Array(count);
            for (let index = 0; index < count; index++) {
                block[index] = input.readInt16LE((offset + index) * 2) / 32768;
            }
            try {
                processor.process([[block]], [], {});
            } finally {
                block.fill(0);
            }
        }

        if (postedSamples < targetSamples) {
            fail(EXIT_CORPUS_INVALID);
            return;
        }
        combined = Buffer.concat(posted);
        selected = Buffer.allocUnsafe(targetSamples * 2);
        combined.copy(selected, 0, 0, selected.length);
        await new Promise((resolve, reject) => {
            process.stdout.write(selected, (error) => {
                selected.fill(0);
                if (error) {
                    reject(error);
                } else {
                    resolve();
                }
            });
        });
    } catch (_error) {
        fail(EXIT_BROWSER_UNAVAILABLE);
    } finally {
        input.fill(0);
        if (combined !== null) {
            combined.fill(0);
        }
        if (selected !== null) {
            selected.fill(0);
        }
        if (processor !== null) {
            if (processor.buffer instanceof Float32Array) {
                processor.buffer.fill(0);
            }
            if (processor.lowPassHistory instanceof Float32Array) {
                processor.lowPassHistory.fill(0);
            }
            processor.resampleTailSample = 0;
            processor.resamplePosition = 0;
            processor.bufferIndex = 0;
            processor.lowPassHistoryFilled = 0;
            processor.hasResampleTail = false;
        }
        for (const chunk of posted) {
            chunk.fill(0);
        }
        posted.length = 0;
        processor = null;
    }
}

main().catch(() => fail(EXIT_BROWSER_UNAVAILABLE));
