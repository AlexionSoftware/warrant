import base64
import binascii
import datetime
import hashlib
import hmac
import re
from typing import Any, Optional

import boto3
import os
import six

from .exceptions import ForceChangePasswordException, MFATokenRequiredException

# https://github.com/aws/amazon-cognito-identity-js/blob/master/src/AuthenticationHelper.js#L22
n_hex = 'FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1' + '29024E088A67CC74020BBEA63B139B22514A08798E3404DD' + \
        'EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245' + 'E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED' + \
        'EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D' + 'C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F' + \
        '83655D23DCA3AD961C62F356208552BB9ED529077096966D' + '670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B' + \
        'E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9' + 'DE2BCBF6955817183995497CEA956AE515D2261898FA0510' + \
        '15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64' + 'ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7' + \
        'ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B' + 'F12FFA06D98A0864D87602733EC86A64521F2B18177B200C' + \
        'BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31' + '43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF'
# https://github.com/aws/amazon-cognito-identity-js/blob/master/src/AuthenticationHelper.js#L49
g_hex = '2'
info_bits = bytearray('Caldera Derived Key', 'utf-8')


def hash_sha256(buf: Any) -> str:
    """AuthenticationHelper.hash"""
    a = hashlib.sha256(buf).hexdigest()
    return (64 - len(a)) * '0' + a


def hex_hash(hex_string: str) -> str:
    return hash_sha256(bytearray.fromhex(hex_string))


def hex_to_long(hex_string: str) -> int:
    return int(hex_string, 16)


def long_to_hex(long_num: int) -> str:
    return '%x' % long_num


def get_random(nbytes: bytes) -> int:
    random_hex = binascii.hexlify(os.urandom(nbytes))
    return hex_to_long(random_hex)


def pad_hex(long_int: int) -> str:
    """
    Converts a Long integer (or hex string) to hex format padded with zeroes for hashing
    :param {Long integer|String} long_int Number or string to pad.
    :return {String} Padded hex string.
    """
    if not isinstance(long_int, six.string_types):
        hash_str = long_to_hex(long_int)
    else:
        hash_str = long_int
    if len(hash_str) % 2 == 1:
        hash_str = '0%s' % hash_str
    elif hash_str[0] in '89ABCDEFabcdef':
        hash_str = '00%s' % hash_str
    return hash_str


def compute_hkdf(ikm: Any, salt: bytes) -> bytes:
    """
    Standard hkdf algorithm
    :param {Buffer} ikm Input key material.
    :param {Buffer} salt Salt value.
    :return {Buffer} Strong key material.
    @private
    """
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    info_bits_update = info_bits + bytearray(chr(1), 'utf-8')
    hmac_hash = hmac.new(prk, info_bits_update, hashlib.sha256).digest()
    return hmac_hash[:16]


def calculate_u(big_a: int, big_b: int) -> int:
    """
    Calculate the client's value U which is the hash of A and B
    :param {Long integer} big_a Large A value.
    :param {Long integer} big_b Server B value.
    :return {Long integer} Computed U value.
    """
    u_hex_hash = hex_hash(pad_hex(big_a) + pad_hex(big_b))
    return hex_to_long(u_hex_hash)


class ICognitoClient:
    """ Interface voor Boto3 CognitoClient
    """
    def initiate_auth(self, AuthFlow: str, AuthParameters: dict, ClientId: str) -> dict:
        ...
    def respond_to_auth_challenge(self, ClientId: str, ChallengeName: str, ChallengeResponses: dict[str, str]) -> dict[str, str]:
        ...



class AWSSRP:

    NEW_PASSWORD_REQUIRED_CHALLENGE = 'NEW_PASSWORD_REQUIRED'
    PASSWORD_VERIFIER_CHALLENGE = 'PASSWORD_VERIFIER'
    DEVICE_SRP_AUTH = 'DEVICE_SRP_AUTH'
    SOFTWARE_TOKEN_MFA_CHALLENGE = 'SOFTWARE_TOKEN_MFA'

    def __init__(self, username: str, password: str, pool_id: str, client_id: str, pool_region: Optional[str]=None,
                 client: Optional[ICognitoClient]=None, client_secret: Optional[str]=None,
                 device_key: Optional[str]=None, device_password: Optional[str]=None, device_group_key: Optional[str]=None,):
        if pool_region is not None and client is not None:
            raise ValueError("pool_region and client should not both be specified "
                             "(region should be passed to the boto3 client instead)")

        self.username = username
        self.password = password
        self.pool_id = pool_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.device_key = device_key
        self.device_group_key = device_group_key
        self.device_password = device_password

        self.client = client if client else boto3.client('cognito-idp', region_name=pool_region)
        self.big_n = hex_to_long(n_hex)
        self.g = hex_to_long(g_hex)
        self.k = hex_to_long(hex_hash('00' + n_hex + '0' + g_hex))
        self.small_a_value = self.generate_random_small_a()
        self.large_a_value = self.calculate_a()

    def generate_random_small_a(self) -> int:
        """
        helper function to generate a random big integer
        :return {Long integer} a random value.
        """
        random_long_int = get_random(128)
        return random_long_int % self.big_n

    def calculate_a(self) -> int:
        """
        Calculate the client's public value A = g^a%N
        with the generated random number a
        :param {Long integer} a Randomly generated small A.
        :return {Long integer} Computed large A.
        """
        big_a = pow(self.g, self.small_a_value, self.big_n)
        # safety check
        if (big_a % self.big_n) == 0:
            raise ValueError('Safety check for A failed')
        return big_a

    def get_password_authentication_key(self, username: str, password: str, server_b_value: int, salt: str) -> bytes:
        """
        Calculates the final hkdf based on computed S value, and computed U value and the key
        :param {String} username Username.
        :param {String} password Password.
        :param {Long integer} server_b_value Server B value.
        :param {Long integer} salt Generated salt.
        :return {Buffer} Computed HKDF value.
        """
        u_value = calculate_u(self.large_a_value, server_b_value)
        if u_value == 0:
            raise ValueError('U cannot be zero.')
        username_password = '%s%s:%s' % (self.pool_id.split('_')[1], username, password)
        username_password_hash = hash_sha256(username_password.encode('utf-8'))

        x_value = hex_to_long(hex_hash(pad_hex(salt) + username_password_hash))
        g_mod_pow_xn = pow(self.g, x_value, self.big_n)
        int_value2 = server_b_value - self.k * g_mod_pow_xn
        s_value = pow(int_value2, self.small_a_value + u_value * x_value, self.big_n)
        hkdf = compute_hkdf(bytearray.fromhex(pad_hex(s_value)),
                            bytearray.fromhex(pad_hex(long_to_hex(u_value))))
        return hkdf

    def get_auth_params(self) -> dict[str, str]:
        auth_params = {'USERNAME': self.username,
                       'SRP_A': long_to_hex(self.large_a_value)}
        if self.client_secret is not None:
            auth_params.update({
                "SECRET_HASH":
                self.get_secret_hash(self.username, self.client_id, self.client_secret)})
        if self.device_key is not None:
            auth_params.update({"DEVICE_KEY": self.device_key})
        return auth_params

    @staticmethod
    def get_secret_hash(username: str, client_id: str, client_secret: str) -> str:
        message = bytearray(username + client_id, 'utf-8')
        hmac_obj = hmac.new(bytearray(client_secret, 'utf-8'), message, hashlib.sha256)
        return base64.standard_b64encode(hmac_obj.digest()).decode('utf-8')

    def process_challenge(self, challenge_parameters: dict[str, str]) -> dict[str, str]:
        user_id_for_srp = challenge_parameters['USER_ID_FOR_SRP']
        salt_hex = challenge_parameters['SALT']
        srp_b_hex = challenge_parameters['SRP_B']
        secret_block_b64 = challenge_parameters['SECRET_BLOCK']
        # re strips leading zero from a day number (required by AWS Cognito)
        timestamp = re.sub(r" 0(\d) ", r" \1 ",
                           datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y"))
        hkdf = self.get_password_authentication_key(user_id_for_srp,
                                                    self.password, hex_to_long(srp_b_hex), salt_hex)
        secret_block_bytes = base64.standard_b64decode(secret_block_b64)
        msg = bytearray(self.pool_id.split('_')[1], 'utf-8') + bytearray(user_id_for_srp, 'utf-8') + \
            bytearray(secret_block_bytes) + bytearray(timestamp, 'utf-8')
        hmac_obj = hmac.new(hkdf, msg, digestmod=hashlib.sha256)
        signature_string = base64.standard_b64encode(hmac_obj.digest())
        response = {'TIMESTAMP': timestamp,
                    'USERNAME': user_id_for_srp,
                    'PASSWORD_CLAIM_SECRET_BLOCK': secret_block_b64,
                    'PASSWORD_CLAIM_SIGNATURE': signature_string.decode('utf-8')}
        if self.client_secret is not None:
            response.update({
                "SECRET_HASH":
                self.get_secret_hash(self.username, self.client_id, self.client_secret)})
        if self.device_key is not None:
            response["DEVICE_KEY"] = self.device_key
        return response

    def authenticate_user(self, client: Optional[ICognitoClient]=None) -> dict[str, str]:
        boto_client = self.client or client
        auth_params = self.get_auth_params()
        response = boto_client.initiate_auth(
            AuthFlow='USER_SRP_AUTH',
            AuthParameters=auth_params,
            ClientId=self.client_id
        )
        if response['ChallengeName'] == self.PASSWORD_VERIFIER_CHALLENGE:
            challenge_response = self.process_challenge(response['ChallengeParameters'])
            tokens = boto_client.respond_to_auth_challenge(
                ClientId=self.client_id,
                ChallengeName=self.PASSWORD_VERIFIER_CHALLENGE,
                ChallengeResponses=challenge_response)
            if tokens.get('ChallengeName') == self.DEVICE_SRP_AUTH:
                return self._authenticate_device(boto_client, tokens)
            if tokens.get('ChallengeName') == self.NEW_PASSWORD_REQUIRED_CHALLENGE:
                raise ForceChangePasswordException('Change password before authenticating')
            if tokens.get('ChallengeName') == self.SOFTWARE_TOKEN_MFA_CHALLENGE:
                raise MFATokenRequiredException('Software MFA Token required to authenticate')

            return tokens
        else:
            raise NotImplementedError('The %s challenge is not supported' % response['ChallengeName'])

    def authenticate_user_with_mfa_token(self, mfaToken: str, client: Optional[ICognitoClient]=None) -> dict[str, str]:
        boto_client = self.client or client
        auth_params = self.get_auth_params()
        response = boto_client.initiate_auth(
            AuthFlow='USER_SRP_AUTH',
            AuthParameters=auth_params,
            ClientId=self.client_id
        )
        userSub = response["ChallengeParameters"]["USERNAME"]
        if response['ChallengeName'] == self.PASSWORD_VERIFIER_CHALLENGE:
            challenge_response = self.process_challenge(response['ChallengeParameters'])
            tokens = boto_client.respond_to_auth_challenge(
                ClientId=self.client_id,
                ChallengeName=self.PASSWORD_VERIFIER_CHALLENGE,
                ChallengeResponses=challenge_response)

            if tokens.get('ChallengeName') == self.SOFTWARE_TOKEN_MFA_CHALLENGE:
                challenge_response = {
                    'USERNAME': userSub,
                    'SOFTWARE_TOKEN_MFA_CODE': mfaToken,
                }
                mfa_response = boto_client.respond_to_auth_challenge(
                    ClientId=self.client_id,
                    ChallengeName=self.SOFTWARE_TOKEN_MFA_CHALLENGE,
                    Session=tokens['Session'],
                    ChallengeResponses=challenge_response)
                return mfa_response
            return tokens
        else:
            raise NotImplementedError('The %s challenge is not supported' % response['ChallengeName'])

    def set_new_password_challenge(self, new_password: str, client: Optional[ICognitoClient]=None) -> dict[str, str]:
        boto_client = self.client or client
        auth_params = self.get_auth_params()
        response = boto_client.initiate_auth(
            AuthFlow='USER_SRP_AUTH',
            AuthParameters=auth_params,
            ClientId=self.client_id
        )
        if response['ChallengeName'] == self.PASSWORD_VERIFIER_CHALLENGE:
            challenge_response = self.process_challenge(response['ChallengeParameters'])
            tokens = boto_client.respond_to_auth_challenge(
                ClientId=self.client_id,
                ChallengeName=self.PASSWORD_VERIFIER_CHALLENGE,
                ChallengeResponses=challenge_response)

            if tokens['ChallengeName'] == self.NEW_PASSWORD_REQUIRED_CHALLENGE:
                challenge_response = {
                    'USERNAME': auth_params['USERNAME'],
                    'NEW_PASSWORD': new_password
                }
                new_password_response = boto_client.respond_to_auth_challenge(
                    ClientId=self.client_id,
                    ChallengeName=self.NEW_PASSWORD_REQUIRED_CHALLENGE,
                    Session=tokens['Session'],
                    ChallengeResponses=challenge_response)
                return new_password_response
            return tokens
        else:
            raise NotImplementedError('The %s challenge is not supported' % response['ChallengeName'])

    @staticmethod
    def generate_hash_device(device_group_key: str, device_key: str) -> tuple[str, str]:
        """ Generate information for devices
        """
        # source: https://github.com/amazon-archives/amazon-cognito-identity-js/blob/6b87f1a30a998072b4d98facb49dcaf8780d15b0/src/AuthenticationHelper.js#L137

        # random device password, which will be used for DEVICE_SRP_AUTH flow
        device_password = base64.standard_b64encode(os.urandom(40)).decode('utf-8')

        combined_string = '%s%s:%s' % (device_group_key, device_key, device_password)
        combined_string_hash = hash_sha256(combined_string.encode('utf-8'))
        salt = pad_hex(get_random(16))

        x_value = hex_to_long(hex_hash(salt + combined_string_hash))
        g = hex_to_long(g_hex)
        big_n = hex_to_long(n_hex)
        verifier_device_not_padded = pow(g, x_value, big_n)
        verifier = pad_hex(verifier_device_not_padded)

        device_secret_verifier_config = {
            "PasswordVerifier": base64.standard_b64encode(bytearray.fromhex(verifier)).decode('utf-8'),
            "Salt": base64.standard_b64encode(bytearray.fromhex(salt)).decode('utf-8')
        }
        return device_password, device_secret_verifier_config

    def _authenticate_device(self, boto_client: ICognitoClient, response: dict) -> dict[str, str]:
        auth_params = self.get_auth_params()

        # Note that device auth flow doesn't start with client.initiate_auth(),
        # but rather with client.respond_to_auth_challenge() straight away
        response_auth = boto_client.respond_to_auth_challenge(
            ClientId=self.client_id,
            ChallengeName='DEVICE_SRP_AUTH',
            ChallengeResponses=auth_params,
        )

        cr = self.process_device_challenge(response_auth['ChallengeParameters'])
        response_verifier = boto_client.respond_to_auth_challenge(
            ClientId=self.client_id,
            ChallengeName='DEVICE_PASSWORD_VERIFIER',
            ChallengeResponses=cr
        )
        return response_verifier

    def get_device_authentication_key(self, device_group_key: str, device_key: str, device_password: str, server_b_value: int, salt: str) -> int:
        u_value = calculate_u(self.large_a_value, server_b_value)
        if u_value == 0:
            raise ValueError('U cannot be zero.')
        username_password = '%s%s:%s' % (device_group_key, device_key, device_password)
        username_password_hash = hash_sha256(username_password.encode('utf-8'))

        x_value = hex_to_long(hex_hash(pad_hex(salt) + username_password_hash))
        g_mod_pow_xn = pow(self.g, x_value, self.big_n)
        int_value2 = server_b_value - self.k * g_mod_pow_xn
        s_value = pow(int_value2, self.small_a_value + u_value * x_value, self.big_n)
        hkdf = compute_hkdf(bytearray.fromhex(pad_hex(s_value)),
                            bytearray.fromhex(pad_hex(long_to_hex(u_value))))
        return hkdf

    def process_device_challenge(self, challenge_parameters: dict) -> dict:
        username = challenge_parameters['USERNAME']
        salt_hex = challenge_parameters['SALT']
        srp_b_hex = challenge_parameters['SRP_B']
        secret_block_b64 = challenge_parameters['SECRET_BLOCK']
        # re strips leading zero from a day number (required by AWS Cognito)
        timestamp = re.sub(r" 0(\d) ", r" \1 ",
                           datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y"))
        hkdf = self.get_device_authentication_key(self.device_group_key,
                                                  self.device_key,
                                                  self.device_password,
                                                  hex_to_long(srp_b_hex),
                                                  salt_hex)
        secret_block_bytes = base64.standard_b64decode(secret_block_b64)
        msg = bytearray(self.device_group_key, 'utf-8') + bytearray(self.device_key, 'utf-8') + \
              bytearray(secret_block_bytes) + bytearray(timestamp, 'utf-8')
        hmac_obj = hmac.new(hkdf, msg, digestmod=hashlib.sha256)
        signature_string = base64.standard_b64encode(hmac_obj.digest())
        response = {'TIMESTAMP': timestamp,
                    'USERNAME': username,
                    'PASSWORD_CLAIM_SECRET_BLOCK': secret_block_b64,
                    'PASSWORD_CLAIM_SIGNATURE': signature_string.decode('utf-8'),
                    'DEVICE_KEY': self.device_key}
        if self.client_secret is not None:
            response.update({
                "SECRET_HASH":
                    self.get_secret_hash(username, self.client_id, self.client_secret)})
        return response
