#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import pickle
import siggy
from operator import itemgetter

from flask import jsonify, request
from sqlalchemy import desc
from datetime import datetime

from .startup import app, db
from .node import (create_token, get_chunk_contract,
                   verify_proof,  update_contract,
                   lookup_contract)
from .models import Token
from .exc import InvalidParameterError, NotFoundError, HttpHandler


@app.route('/')
def api_index():
    return jsonify(msg='ok')


@app.route('/status/list/',
           defaults={'o': False, 'd': False, 'sortby': 'id',
                     'limit': None, 'page': None})
@app.route('/status/list/<int:limit>',
           defaults={'o': False, 'd': False, 'sortby': 'id', 'page': None})
@app.route('/status/list/<int:limit>/<int:page>',
           defaults={'o': False, 'd': False, 'sortby': 'id'})
@app.route('/status/list/by/<sortby>',
           defaults={'o': False, 'd': False, 'limit': None, 'page': None})
@app.route('/status/list/by/d/<sortby>',
           defaults={'o': False, 'd': True, 'limit': None, 'page': None})
@app.route('/status/list/by/<sortby>/<int:limit>',
           defaults={'o': False, 'd': False, 'page': 0})
@app.route('/status/list/by/d/<sortby>/<int:limit>',
           defaults={'o': False, 'd': True, 'page': 0})
@app.route('/status/list/by/<sortby>/<int:limit>/<int:page>',
           defaults={'o': False, 'd': False})
@app.route('/status/list/by/d/<sortby>/<int:limit>/<int:page>',
           defaults={'o': False, 'd': True})
@app.route('/status/list/online/',
           defaults={'o': True, 'd': False, 'sortby': 'id',
                     'limit': None, 'page': None})
@app.route('/status/list/online/<int:limit>',
           defaults={'o': True, 'd': False, 'sortby': 'id', 'page': None})
@app.route('/status/list/online/<int:limit>/<int:page>',
           defaults={'o': True, 'd': False, 'sortby': 'id'})
@app.route('/status/list/online/by/<sortby>',
           defaults={'o': True, 'd': False, 'limit': None, 'page': None})
@app.route('/status/list/online/by/d/<sortby>',
           defaults={'o': True, 'd': True, 'limit': None, 'page': None})
@app.route('/status/list/online/by/<sortby>/<int:limit>',
           defaults={'o': True, 'd': False, 'page': 0})
@app.route('/status/list/online/by/d/<sortby>/<int:limit>',
           defaults={'o': True, 'd': True, 'page': 0})
@app.route('/status/list/online/by/'
           '<sortby>/<int:limit>/<int:page>',
           defaults={'o': True, 'd': False})
@app.route('/status/list/online/by/d/'
           '<sortby>/<int:limit>/<int:page>',
           defaults={'o': True, 'd': True})
def api_downstream_status_list(o, d, sortby, limit, page):
    with HttpHandler(app.mongo_logger) as handler:
        sort_map = {'id': Token.farmer_id,
                    'address': Token.addr,
                    'uptime': None,
                    'heartbeats': Token.hbcount,
                    'contracts': Token.contract_count,
                    'size': Token.size,
                    'online': Token.online}

        if (sortby not in sort_map):
            raise InvalidParameterError('Invalid sort.')
        # we need to sort uptime manually
        # what we're doing here is going through each farmer's contracts. it
        # sums up the time that the farmer has been online, and then divides
        # by the total time the farmer has had any contracts.
        if o:
            all_tokens = Token.query.filter(Token.online).all()
        else:
            all_tokens = Token.query.all()
            
        uptimes = list()
        utdict = dict()
        for t in all_tokens:
            ut = t.uptime
            utdict[t.id] = ut
            if (d):
                uptimes.append(-ut)
            else:
                uptimes.append(ut)
                
        if (sortby == 'uptime'):
            (uptimes, farmer_list) = zip(*sorted(zip(uptimes, all_tokens), key=itemgetter(0)))
            
            if (page is not None):
                farmer_list = farmer_list[limit * page:limit * page + limit]

            if (limit is not None):
                farmer_list = farmer_list[:limit]
        else:
            sort_stmt = sort_map[sortby]
            if (d):
                sort_stmt = desc(sort_stmt)

            farmer_list_query = Token.query

            if (o):
                farmer_list_query = farmer_list_query.filter(Token.online)

            farmer_list_query = farmer_list_query.order_by(sort_stmt)

            if (limit is not None):
                farmer_list_query = farmer_list_query.limit(limit)

            if (page is not None):
                farmer_list_query = farmer_list_query.offset(limit * page)

            farmer_list = farmer_list_query.all()

        farmers = [dict(id=a.farmer_id,
                        address=a.addr,
                        location=a.location,
                        uptime=round(utdict[a.id] * 100, 2),
                        heartbeats=a.hbcount,
                        contracts=a.contract_count,
                        last_due=a.last_due,
                        size=a.size,
                        online=a.online) for a in farmer_list]

        db.session.commit()
        
        return jsonify(farmers=farmers)

    return handler.response


@app.route('/status/show/<farmer_id>')
def api_downstream_status_show(farmer_id):
    with HttpHandler(app.mongo_logger) as handler:
        a = Token.query.filter(Token.farmer_id == farmer_id).first()

        if (a is None):
            raise NotFoundError('Nonexistant farmer id.')

        response = dict(id=a.farmer_id,
                        address=a.addr,
                        location=a.location,
                        uptime=round(a.uptime * 100, 2),
                        heartbeats=a.hbcount,
                        contracts=a.contract_count,
                        last_due=a.last_due,
                        size=a.size,
                        online=a.online)

        return jsonify(response)

    return handler.response


@app.route('/new/<sjcx_address>', methods=['GET', 'POST'])
def api_downstream_new_token(sjcx_address):
    # generate a new token
    with HttpHandler(app.mongo_logger) as handler:
        handler.context['sjcx_address'] = sjcx_address
        handler.context['remote_addr'] = request.remote_addr

        message = None
        signature = None
        if (app.config['REQUIRE_SIGNATURE']):
            if (request.method == 'POST'):
                # need to have a restriction on posted data size....
                # for now, we'll restrict message length
                d = request.get_json(silent=True)

                # put the posted data into the context for logging
                handler.context['posted_data'] = d

                if (d is False or not isinstance(d, dict)
                        or 'signature' not in d or 'message' not in d):
                    raise InvalidParameterError(
                        'Posted data must be JSON encoded object including '
                        '"signature" and "message"')

                if (len(d['message']) > app.config['MAX_SIG_MESSAGE_SIZE']):
                    raise InvalidParameterError(
                        'Please restrict your message to less than {0} bytes.'
                        .format(app.config['MAX_SIG_MESSAGE_SIZE']))

                if (len(d['signature']) != siggy.SIGNATURE_LENGTH):
                    raise InvalidParameterError(
                        'Your signature is the wrong length.  It should be {0}'
                        'bytes.'.format(siggy.SIGNATURE_LENGTH))

                message = d['message']
                signature = d['signature']

                # parse the signature and message
                if (not siggy.verify_signature(message,
                                               signature,
                                               sjcx_address)):
                    raise InvalidParameterError('Signature invalid.')
            else:
                raise InvalidParameterError(
                    'New token requests must include posted signature proving '
                    'ownership of farming address')

        db_token = create_token(
            sjcx_address, request.remote_addr, message, signature)
        beat = db_token.heartbeat
        pub_beat = beat.get_public()

        response = dict(token=db_token.token,
                        type=type(beat).__name__,
                        heartbeat=pub_beat.todict())

        if (app.mongo_logger is not None):
            app.mongo_logger.log_event('new', {'context': handler.context,
                                               'response': response})

        return jsonify(response)

    return handler.response


@app.route('/heartbeat/<token>')
def api_downstream_heartbeat(token):
    """This route gets the heartbeat for a token.
    The heartbeat is the object that contains data for proving
    existence of a file (for example, Swizzle, Merkle objects)
    Provided for nodes that need to recover their heartbeat.
    The heartbeat does not contain any private information,
    so having someone else's heartbeat does not help you.
    """
    with HttpHandler(app.mongo_logger) as handler:
        handler.context['token'] = token
        handler.context['remote_addr'] = request.remote_addr
        db_token = Token.query.filter(Token.token == token).first()

        if (db_token is None):
            raise NotFoundError('Nonexistent token.')

        beat = db_token.heartbeat
        pub_beat = beat.get_public()
        response = dict(token=db_token.token,
                        type=type(beat).__name__,
                        heartbeat=pub_beat.todict())

        if (app.mongo_logger is not None):
            app.mongo_logger.log_event('heartbeat',
                                       {'context': handler.context,
                                        'response': response})

        return jsonify(response)

    return handler.response


@app.route('/chunk/<token>',
           defaults={'size': app.config['DEFAULT_CHUNK_SIZE']})
@app.route('/chunk/<token>/<int:size>')
def api_downstream_chunk_contract(token, size):
    with HttpHandler(app.mongo_logger) as handler:
        handler.context['token'] = token
        handler.context['size'] = size
        handler.context['remote_addr'] = request.remote_addr

        db_contract = get_chunk_contract(token, size, request.remote_addr)

        with open(db_contract.tag_path, 'rb') as f:
            tag = pickle.load(f)
        chal = db_contract.challenge

        # now since we are prototyping, we can delete the tag and file
        os.remove(db_contract.file.path)
        os.remove(db_contract.tag_path)

        response = dict(seed=db_contract.seed,
                        size=db_contract.size,
                        file_hash=db_contract.file.hash,
                        challenge=chal.todict(),
                        tag=tag.todict(),
                        due=(db_contract.due - datetime.utcnow()).
                        total_seconds())

        if (app.mongo_logger is not None):
            # we'll remove the tag becauase it could potentially be very large
            rsummary = {key: response[key] for key in ['seed',
                                                       'size',
                                                       'file_hash',
                                                       'due',
                                                       'challenge']}
            rsummary['tag'] = 'REDACTED'
            app.mongo_logger.log_event('chunk',
                                       {'context': handler.context,
                                        'response': rsummary})

        return jsonify(response)

    return handler.response


@app.route('/challenge/<token>/<file_hash>')
def api_downstream_chunk_contract_status(token, file_hash):
    """For prototyping, this will generate a new challenge
    """
    with HttpHandler(app.mongo_logger) as handler:
        handler.context['token'] = token
        handler.context['file_hash'] = file_hash
        handler.context['remote_addr'] = request.remote_addr
        db_contract = update_contract(token, file_hash)

        response = dict(challenge=db_contract.challenge.todict(),
                        due=(db_contract.due - datetime.utcnow()).
                        total_seconds(),
                        answered=db_contract.answered)

        if (app.mongo_logger is not None):
            app.mongo_logger.log_event('challenge',
                                       {'context': handler.context,
                                        'response': response})

        return jsonify(response)

    return handler.response


@app.route('/answer/<token>/<file_hash>', methods=['POST'])
def api_downstream_challenge_answer(token, file_hash):
    with HttpHandler(app.mongo_logger) as handler:
        handler.context['token'] = token
        handler.context['file_hash'] = file_hash
        handler.context['remote_addr'] = request.remote_addr
        d = request.get_json(silent=True)

        if (d is False or not isinstance(d, dict) or 'proof' not in d):
            raise InvalidParameterError('Posted data must be an JSON encoded '
                                        'proof object: '
                                        '{"proof":"...proof object..."}')

        db_contract = lookup_contract(token, file_hash)

        beat = db_contract.token.heartbeat

        try:
            proof = beat.proof_type().fromdict(d['proof'])
        except:
            raise InvalidParameterError('Proof corrupted.')

        if (not verify_proof(token, file_hash, proof, request.remote_addr)):
            raise InvalidParameterError(
                'Invalid proof.')

        response = dict(status='ok')

        if (app.mongo_logger is not None):
            app.mongo_logger.log_event('answer',
                                       {'context': handler.context,
                                        'response': response})

        return jsonify(response)

    return handler.response
